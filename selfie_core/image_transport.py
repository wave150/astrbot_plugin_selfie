"""Compatibility image-service adapter: one OpenAI-compatible primary + optional backup.

Transport concerns are isolated from message routing and world policies.
PhotoUseCase accepts any async generator with the same keyword contract.
This mixin uses host configuration/reference selectors; it does not send chat messages.
"""
import asyncio
import base64
import os
import re
import aiohttp
from astrbot.api import logger
from .defaults import *
from .network import download_public_image, decode_image_base64

class ImageTransportMixin:
    def _api(self) -> tuple[str, str]:
        api_base = str(self._cfg("api_base", "") or "").strip().rstrip("/")
        api_key = str(self._cfg("api_key", "") or "").strip()
        if api_base and api_key:
            return api_base, api_key
        # 只使用在插件里显式配置的生图主站/副站，不再借用其他模型服务的凭据。
        return "", ""


    @staticmethod
    def _is_rate_limit_error(err) -> bool:
        """是不是「站点在限流」：这种错误重试只会更糟，应该直接跳过这一家。"""
        s = str(err or "").lower()
        for kw in ("temporarily unavailable", "rate limit", "rate_limit",
                   "too many requests", "429"):
            if kw in s:
                return True
        return False


    def _allow_text_only(self) -> bool:
        """是否允许「没有参考图也硬出图」（纯文生图）。默认 False —— 纯文生图根本不像她。"""
        return bool(self._cfg("allow_text_only", False))


    def _site_max_refs(self) -> int:
        """当前所有站点里能给到的最多参考图张数（单图站点=1）。"""
        try:
            sites = self._image_sites()
            return max((int(s[2]) for s in sites), default=2) if sites else 2
        except Exception:
            return 2


    def _image_sites(self) -> list:
        """生图站点列表：[(api_base, api_key, max_refs), ...]，主站在前、备用站在后。

        max_refs：这个站最多吃几张参考图。实测备用站 initapi 只吃 1 张（2 张直接 500）。
        """
        sites = []
        base, key = self._api()
        if base and key:
            try:
                mr = int(self._cfg("image_max_refs", 2) or 2)
            except Exception:
                mr = 2
            sites.append((base, key, max(0, mr)))
        b2 = str(self._cfg("api_base_backup", "") or "").strip().rstrip("/")
        k2 = str(self._cfg("api_key_backup", "") or "").strip()
        if b2 and k2:
            try:
                mr2 = int(self._cfg("image_max_refs_backup", 1) or 1)
            except Exception:
                mr2 = 1
            sites.append((b2, k2, max(0, mr2)))
        return sites


    def _image_plan(self, model: str) -> list:
        """出图尝试计划：[主站（模型链，双图），之后副站（只一次，单图）]。"""
        sites = self._image_sites()
        if not sites:
            return []
        models = [model]
        for m in self._image_fallback_models():
            if m and m not in models:
                models.append(m)
        plan = []
        base, key, max_refs = sites[0]
        for m in models:
            plan.append((base, key, max_refs, m, "primary"))
        if len(sites) > 1:
            b2, k2, r2 = sites[1]
            plan.append((b2, k2, r2, model, "backup"))
        return plan


    def _image_targets(self, model: str) -> list:
        """「站点 × 模型」交替序列：A.主模型 -> B.主模型 -> A.备用1 -> B.备用1 ..."""
        models = [model]
        for m in self._image_fallback_models():
            if m and m not in models:
                models.append(m)
        sites = self._image_sites()
        if not sites:
            return []
        return [(b, k, r, m) for m in models for (b, k, r) in sites]


    def _image_fallback_models(self) -> list:
        """主模型抽风时依次降级的备用模型（配置 image_model_fallback，逗号分隔）。"""
        raw = str(self._cfg("image_model_fallback", "") or "").strip()
        if raw:
            return [x.strip() for x in re.split(r"[,，、\s]+", raw) if x.strip()]
        return list(DEFAULT_IMAGE_FALLBACK_MODELS)


    @staticmethod
    def _is_quota_error(err) -> bool:
        """这次失败是不是「没额度/没余额」——是的话回「手机没电了」而不是让她道歉。

        中转平台的写法比较杂：402 是明确的余额不足；额度耗光时也可能回 403，
        正文里带 Insufficient user quota / BALANCE_INSUFFICIENT 之类。
        """
        s = str(err or "").lower()
        # 注意：不要把裸 "403" 当成额度信号 —— 中转平台抽风时也用 403
        # （upstream_request_rejected），那属于「换条通道还能救」，不是没额度。
        for kw in ("balance_insufficient", "insufficient", "quota", "余额",
                   "402", "no credit", "credit_grants"):
            if kw in s:
                return True
        return False








    async def _generate_impl(self, prompt: str, model: str,
                        style: str | None = None,
                        negative_extra: list[str] | None = None,
                        extra_images: list[str] | None = None) -> bytes:
        api_base, api_key = self._api()
        if not api_base or not api_key:
            raise RuntimeError("没有配置生图主站地址或密钥")
        # 只用 OpenAI 兼容接口：主站（模型链）-> 副站（只试一次）
        if True:
            # 用户定的策略：主站（双图，模型链）-> 副站（单图，只试一次）-> 失败就报错
            plan = self._image_plan(model)
            if not plan:
                plan = [(api_base, api_key, 2, model, "primary")]
            last_err = None
            # 两次尝试之间拉开间隔（章鱼是因为请求过频被封的，别再猛打）
            try:
                gap = float(self._cfg("image_min_gap_seconds", 12) or 12)
            except Exception:
                gap = 12.0
            dead_sites = set()
            for (base, key, max_refs, mdl, kind) in plan:
                if base in dead_sites:
                    continue
                wait = gap - (self.clock.time() - float(getattr(self, "_last_img_at", 0) or 0))
                if wait > 0:
                    await asyncio.sleep(min(wait, gap))
                self._last_img_at = self.clock.time()
                host = re.sub(r"^https?://", "", base).split("/")[0]
                try:
                    return await self._generate_openai(
                        prompt, mdl, base, key, extra_images, style, 1, max_refs
                    )
                except Exception as e:
                    # 额度不足 / key 失效：换站换模型都没用，直接上抛
                    if self._is_quota_error(e) or "HTTP 401" in str(e):
                        raise
                    last_err = e
                    logger.warning(f"[selfie] {host} / {mdl} 失败（{str(e)[:70]}）")
                    if kind == "backup":
                        # 用户要求：副站也失败就不重试了
                        logger.warning("[selfie] 副站也失败，按设置不再重试，直接失败")
                        raise
                    if self._is_rate_limit_error(e):
                        dead_sites.add(base)
                        logger.warning(f"[selfie] {host} 在限流，跳过这家剩下的尝试")
            raise last_err or RuntimeError("生图失败")




    async def _generate_openai(self, prompt: str, model: str,
                               api_base: str, api_key: str,
                               extra_images: list[str] | None = None,
                               style: str | None = None,
                               max_attempts: int = 6,
                               max_refs: int = 2) -> bytes:
        """标准 OpenAI 图片接口：有参考图走 /images/edits，否则 /images/generations。

        参考图按画风选：二次元版贴二次元三视图，真人版贴真人三视图（真人版再多贴一张脸部特写）。
        中转平台/上游会随机返回 400 upstream_request_rejected 或 5xx（同一请求 400 之后重发就能 200），
        所以这里带重试 + 换站；**有参考图时不再退回纯文生图**（纯文生图不像她），
        只有一张参考图都找不到、或显式打开 allow_text_only 时才走 /images/generations。
        """
        size = str(self._cfg("image_size", DEFAULT_OPENAI_SIZE) or DEFAULT_OPENAI_SIZE)
        headers = {"Authorization": "Bearer " + api_key, "User-Agent": BROWSER_UA}
        timeout = aiohttp.ClientTimeout(total=600)
        ref = self._pick_ref(style, single=(max_refs < 2))
        # 真人风格额外贴一张脸部特写（cos 双图模式已有自己的图序，不掺进去）
        # max_refs=1 时不加这张 —— 备用站只吃一张参考图
        face_ref = ""
        if max_refs >= 2 and not (extra_images and ref):
            face_ref = self._face_ref(style)
        # 0 = 连接异常；400/403 = 中转平台随机的 upstream_request_rejected（同一提示词重试可过）
        # 实测同一个请求 400 之后立刻重发就能 200，所以多给几次机会、退避时间也拉长
        retryable = {0, 400, 403, 408, 425, 429, 500, 502, 503, 504}
        max_attempts = max(1, int(max_attempts))
        backoff = (4, 8, 16, 24, 36)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:

            async def post_generations():
                async with session.post(
                    api_base + "/images/generations",
                    json={"model": model, "prompt": prompt, "size": size, "n": 1},
                ) as resp:
                    return resp.status, await resp.json(content_type=None)

            async def post_edits():
                form = aiohttp.FormData()
                form.add_field("model", model)
                form.add_field("prompt", prompt)
                form.add_field("size", size)
                form.add_field("n", "1")
                imgs = [ref] + ([face_ref] if face_ref else [])
                imgs += [x for x in (extra_images or []) if x and os.path.exists(x)]
                if len(imgs) > max_refs:
                    raise RuntimeError(f"当前服务最多支持 {max_refs} 张参考图，本次需要 {len(imgs)} 张")
                for idx, img_path in enumerate(imgs):
                    try:
                        with open(img_path, "rb") as fh:
                            img_bytes = fh.read()
                    except Exception as e:
                        if idx == 0:
                            raise RuntimeError("当前角色身份参考图读取失败，禁止只使用服装参考图生成") from e
                        logger.warning("[selfie] 可选参考图读取失败，跳过该附加图片")
                        continue
                    form.add_field(
                        "image",
                        img_bytes,
                        filename=f"ref{idx}_{os.path.basename(img_path)}",
                        content_type="image/png",
                    )
                async with session.post(api_base + "/images/edits", data=form) as resp:
                    return resp.status, await resp.json(content_type=None)

            status, data, mode = 0, {}, "generations"
            for attempt in range(1, max_attempts + 1):
                if ref:
                    try:
                        status, data = await post_edits()
                        mode = "edits"
                    except Exception as e:
                        logger.warning(f"[selfie] 参考图接口异常（第{attempt}次）: {e!r}")
                        status = 0
                    if status in (404, 405, 422):
                        if self._allow_text_only():
                            logger.warning(
                                f"[selfie] 参考图接口不可用（HTTP {status}），"
                                "allow_text_only 已开启，退回纯文生图"
                            )
                            status, data = await post_generations()
                            mode = "generations"
                        else:
                            logger.warning(
                                f"[selfie] 参考图接口不可用（HTTP {status}），"
                                "不退回纯文生图（那玩意不像她），按失败处理"
                            )
                else:
                    if not self._allow_text_only():
                        raise RuntimeError("当前角色缺少有效参考图；已禁止无参考图生成，请配置角色身体/头部参考图")
                    status, data = await post_generations()
                    mode = "generations"
                if status == 200:
                    break
                if status not in retryable:
                    break
                logger.warning(
                    f"[selfie] 生图失败（第{attempt}/{max_attempts}次, HTTP {status}），"
                    f"重试: {str(data)[:120]}"
                )
                if (ref and mode == "edits" and attempt == max_attempts
                        and self._allow_text_only()):
                    logger.warning(
                        "[selfie] 参考图路线连续失败，allow_text_only 已开启，退回纯文生图再试一次"
                    )
                    status, data = await post_generations()
                    mode = "generations"
                    if status == 200:
                        break
                # 逐渐拉长间隔，尽量别在同一个「上游抽风窗口」里连撞
                await asyncio.sleep(backoff[min(attempt - 1, len(backoff) - 1)])

            if status == 401:
                raise RuntimeError("API Key 无效或已失效（HTTP 401）")
            if status == 402:
                raise RuntimeError("BALANCE_INSUFFICIENT: 生图账户余额不足（HTTP 402）")
            if status == 429:
                raise RuntimeError("生图接口限流（HTTP 429），稍后再试")
            if status != 200:
                raise RuntimeError(f"生图失败 HTTP {status}: {str(data)[:300]}")

            item = ((data.get("data") or [{}])[0] or {})
            logger.info(
                f"[selfie] 生图完成 provider=openai model={model} size={size} mode={mode}"
            )
            if item.get("b64_json"):
                return decode_image_base64(item["b64_json"])
            url = item.get("url")
            if not url:
                raise RuntimeError(f"生图失败，没有返回图片: {str(data)[:300]}")
            # Download on an independent, unauthenticated session; never send the API key to a CDN.
            return await download_public_image(url)



