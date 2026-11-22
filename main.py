"""真实世界生活相机 · ta的一天（单实例单角色）。

角色住在一座真实城市里，有家、天气、每天的日程和临时事件，
会按时间出门上班、吃饭、换装、睡觉，也在自己生活的现场拍自拍、特写和互动照。

默认预设仍兼容旧角色：一个二次元少女；插件不内置任何城市数据
- 自拍：第一人称手机前置摄像头视角（照片里不出现手机），白天在外、晚上在床上穿睡衣
- 天气：按角色所在地实时查询，下雪穿棉袄堆雪人，夏天晴天去泳池边
- 场景锁定：生成一张照片后，1 小时内地点/场景不变
- 每天有自己的活动；每 2~4 天自动发一条带图 QQ 空间说说
- 普通用户每天最多 2 张，管理员不限次数

版本历史与完整更新日志见仓库根目录的 CHANGELOG.md。
"""

import asyncio
import base64
from datetime import datetime
import json
import os
import random
import re
import sys
import tempfile
import time

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, StarTools

try:  # AstrBot 4.x：追加到本轮用户消息的临时上下文块（不会被写进历史）
    from astrbot.core.agent.message import TextPart
except Exception:  # 老版本没有，退回改 system_prompt
    TextPart = None

try:  # AstrBot package loader
    from .selfie_core.defaults import *
    from .selfie_core.defaults import _COSTUME_EXCLUDE, _COSTUME_RE, _COSTUME_HINT_PAT
    from .selfie_core.world import WorldMixin, WorldNotReady
    from .selfie_core.character import CharacterProfile
    from .selfie_core.portraits import can_change_clothes, event_outfit
    from .selfie_core.storage import JsonStateStore
    from .selfie_core.network import download_public_image, decode_image_base64
    from .selfie_core.runtime import SystemClock, RandomSource
    from .selfie_core.scene_adaptation import adapt_scene_prompt
    from .selfie_core.jobs import TaskSupervisor, PhotoRequest, PhotoUseCase
    from .selfie_core.image_transport import ImageTransportMixin
    from .selfie_core.workspace_host import WorkspaceHostMixin
    from .selfie_core.workspace import HOME_ROOMS
    from .selfie_core.scene_context import scene_context_text, local_datetime, lighting_text, scene_hour
    from .selfie_core.materials import (FILENAMES, SEED_VERSIONS, MaterialStore, ensure_files, merge_seed, parse_materials)
    from .selfie_core.living import photo_variation
    from .selfie_core.event_materials import action_name, active_event, phase_label, scene_label
    from .selfie_core.sleep import sleep_photo
    from .selfie_core.meal import meal_photo, meal_photo_prompt, meal_text
    from .selfie_core import dialogue, ref_swap
    from .selfie_core import ref_library
    from .selfie_core.weather import fetch_daily_mean, parse_daily_mean
except ImportError:  # direct local test import
    from selfie_core.defaults import *
    from selfie_core.defaults import _COSTUME_EXCLUDE, _COSTUME_RE, _COSTUME_HINT_PAT
    from selfie_core.world import WorldMixin, WorldNotReady
    from selfie_core.character import CharacterProfile
    from selfie_core.portraits import can_change_clothes, event_outfit
    from selfie_core.storage import JsonStateStore
    from selfie_core.network import download_public_image, decode_image_base64
    from selfie_core.runtime import SystemClock, RandomSource
    from selfie_core.scene_adaptation import adapt_scene_prompt
    from selfie_core.jobs import TaskSupervisor, PhotoRequest, PhotoUseCase
    from selfie_core.image_transport import ImageTransportMixin
    from selfie_core.workspace_host import WorkspaceHostMixin
    from selfie_core.workspace import HOME_ROOMS
    from selfie_core.scene_context import scene_context_text, local_datetime, lighting_text, scene_hour
    from selfie_core.materials import (FILENAMES, SEED_VERSIONS, MaterialStore, ensure_files, merge_seed, parse_materials)
    from selfie_core.living import photo_variation
    from selfie_core.event_materials import action_name, active_event, phase_label, scene_label
    from selfie_core.sleep import sleep_photo
    from selfie_core.meal import meal_photo, meal_photo_prompt, meal_text
    from selfie_core import dialogue, ref_swap
    from selfie_core import ref_library
    from selfie_core.weather import fetch_daily_mean, parse_daily_mean


class SelfiePlugin(WorkspaceHostMixin, WorldMixin, ImageTransportMixin, Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        # A running instance has one identity snapshot; reload to switch identities.
        self._active_profile = self._profile()
        # 数据目录只用 AstrBot 的标准插件数据目录：可迁移、可审计，不做 cwd 回退
        data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        self.base_data_dir = data_dir
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            pass
        self.data_dir = data_dir
        self.state_path = os.path.join(data_dir, "state.json")
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        # 运行时素材库统一落在数据目录（升级不丢），插件目录里的同名文件只作只读默认值。
        try:
            ensure_files(self.data_dir, self.plugin_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[selfie] 复制默认素材到数据目录失败: {exc!r}")
        # 默认素材扩充（例如菜品 9 → 53 道）只补新条目，不动用户改过的内容。
        for kind, version in SEED_VERSIONS.items():
            try:
                added = merge_seed(self.data_dir, self.plugin_dir, kind, version)
                if added:
                    logger.info(f"[selfie] 素材库补齐默认{kind}条目 {len(added)} 条：{'、'.join(added[:6])}…")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[selfie] 补齐默认素材({kind})失败: {exc!r}")
        # 不再内置任何参考图：长相、发型、衣服全部由用户在工作台「参考图」页上传。
        self.state = {
            "date": "", "count": 0, "next_qzone_at": 0,
            "quota": {"date": "", "users": {}},
            "night_sleepwear": {},
            "qzone_budget": {},
            "selfies": [],
            "cos_pending": {},
            "learn_pending": {},
        }
        self._generating = False
        self._load_state()
        self._task = None
        try:
            self._task = self._spawn_task(self._qzone_loop())
        except Exception as e:
            logger.warning(f"[selfie] 启动空间任务失败: {e}")
        try:
            self._backfill_task = self._spawn_task(self._backfill_visuals())
        except Exception as e:
            self._backfill_task = None
            logger.warning(f"[selfie] 启动照片补自述任务失败: {e}")

    # ------------------------------------------------------------------
    @property
    def clock(self):
        if not hasattr(self, "_clock"):
            self._clock = SystemClock()
        return self._clock

    @clock.setter
    def clock(self, value):
        self._clock = value

    @property
    def rng(self):
        if not hasattr(self, "_rng"):
            self._rng = RandomSource()
        return self._rng

    @rng.setter
    def rng(self, value):
        self._rng = value

    # 一张图从发起请求到收图的最长总时长（秒）。上游排队/卡住时不能无限等：
    # 超过就中止并回一句失败提示，把 _generating 锁放掉，后面的请求才能继续。
    IMAGE_TOTAL_TIMEOUT = 600

    def _spawn_task(self, coro):
        supervisor = getattr(self, "_task_supervisor", None)
        if supervisor is None:
            supervisor = self._task_supervisor = TaskSupervisor(
                on_error=lambda message: logger.error("[selfie] 后台任务失败（详细内容不写入共享日志）"))
        return supervisor.spawn(coro)

    async def _generate(self, prompt: str, model: str, style: str | None = None,
                        negative_extra=None, extra_images=None,
                        ref: str | None = None, face_ref: str | None = None,
                        single_ref: str | None = None,
                        head_only: bool = False,
                        main_refs=None, backup_refs=None, pose_image: str = "") -> bytes:
        use_case = getattr(self, "_photo_use_case", None)
        if use_case is None:
            use_case = self._photo_use_case = PhotoUseCase(self._generate_impl)
        request = PhotoRequest(prompt, model, style,
                               tuple(negative_extra or ()), tuple(extra_images or ()),
                               ref or "", face_ref or "", single_ref or "",
                               head_only=bool(head_only),
                               main_refs=tuple(main_refs or ()),
                               backup_refs=tuple(backup_refs or ()),
                               pose_image=pose_image)
        try:
            return await asyncio.wait_for(
                use_case.execute(request), timeout=float(self.IMAGE_TOTAL_TIMEOUT)
            )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                f"生图超时：{self.IMAGE_TOTAL_TIMEOUT} 秒内没拿到图，已中止"
                "（上游排队或卡住）"
            ) from e

    def _generation_failure_text(self, error, generic: str = "拍糊了，等我缓缓再拍……") -> str:
        """出图失败时回哪句话：额度不足、超时、其它分开说，别让人以为插件没反应。"""
        try:
            if self._is_quota_error(error):
                return BALANCE_FAIL_TEXT
        except Exception:
            pass
        text = str(error or "")
        if text == "线稿参考特写需要支持至少两张参考图的生图站，请检查主站或副站的参考图张数设置":
            return text
        if text == "线稿参考特写不存在，请刷新线稿列表后重试":
            return text
        if "还没有可用的参考图" in text or ("参考图" in text and "上传" in text):
            return ("我还没有可以参考的照片：打开插件页「自拍工作台 → 参考图」，"
                    "上传并填好主站 / 副站参考图，我就能拍了～")
        if (isinstance(error, (asyncio.TimeoutError, TimeoutError))
                or "超时" in text or "超过" in text):
            return "这张拍太久卡住了，我先停下，过一会儿再给你拍～"
        return generic

    def _selfie_match(self, text):
        return SELFIE_PAT.search(text or "")

    def _interaction_match(self, text):
        """语句里出现的第一个互动触发词（摸摸 / 捏捏 / 戳戳 / 揉揉 / 拍拍……）。

        触发词来自内置表 + 互动库行首的动作词：库里加一条「亲亲：…」就自动多一个
        可触发的词。群聊里是否真的接单仍由 _can_trigger 把关。
        """
        if not text:
            return None
        words = self._interact_triggers()
        pattern = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
        return re.search(r"(" + pattern + r")(?:\s*(?:一下|下))?", text)

    def _profile(self):
        active = getattr(self, "_active_profile", None)
        if active is not None:
            return active
        return CharacterProfile.from_config(self.conf)

    def _cfg(self, key, default=None):
        try:
            workspace = getattr(self, '_workspace', None)
            settings = workspace.settings() if workspace is not None else {}
            val = settings[key] if key in settings else (self.conf.get(key, default) if hasattr(self.conf, "get") else default)
            return default if val in (None, "") else val
        except Exception:
            return default

    def _state_repository(self):
        store = getattr(self, "_state_store", None)
        if store is None or str(store.path) != self.state_path:
            store = self._state_store = JsonStateStore(self.state_path)
        return store

    def _load_state(self) -> None:
        try:
            self.state = self._state_repository().load(self.state)
            self._state_load_failed = False
        except Exception as e:
            self._state_load_failed = True
            logger.error(f"[selfie] 状态读取失败，保护原文件且禁止覆盖: {e}")


    def _save_state(self) -> None:
        if getattr(self, "_state_load_failed", False):
            logger.error("[selfie] 原状态未成功加载，拒绝覆盖；请先修复或恢复备份")
            return
        try:
            self._state_repository().save(self.state)
        except Exception as e:
            logger.warning(f"[selfie] 原子保存状态失败: {e}")


    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.localtime())

    # ---------------- 配额：普通成员按账号单独计数 ----------------
    def _daily_limit(self) -> int:
        try:
            n = int(self._cfg("daily_limit", DAILY_LIMIT))
        except Exception:
            n = DAILY_LIMIT
        return max(1, min(99, n))

    def _quota_bucket(self) -> dict:
        today = self._today()
        bucket = self.state.get("quota")
        if not isinstance(bucket, dict) or bucket.get("date") != today:
            bucket = {"date": today, "users": {}}
            self.state["quota"] = bucket
            self._save_state()
        if not isinstance(bucket.get("users"), dict):
            bucket["users"] = {}
        return bucket

    def _quota_used(self, sender: str) -> int:
        sender = str(sender or "").strip()
        if not sender:
            return 0
        try:
            return int(self._quota_bucket()["users"].get(sender, 0) or 0)
        except Exception:
            return 0

    def _quota_left(self, sender: str = "") -> int:
        return max(0, self._daily_limit() - self._quota_used(sender))

    def _bump_quota(self, sender: str = "") -> None:
        sender = str(sender or "").strip()
        if not sender:
            return
        bucket = self._quota_bucket()
        bucket["users"][sender] = int(bucket["users"].get(sender, 0) or 0) + 1
        self._save_state()

    # ---------------- 空间说说：不占成员额度，单独限流 ----------------
    def _qzone_budget_left(self) -> bool:
        today = self._today()
        rec = self.state.get("qzone_budget")
        if not isinstance(rec, dict) or rec.get("date") != today:
            rec = {"date": today, "count": 0}
            self.state["qzone_budget"] = rec
            self._save_state()
        try:
            return int(rec.get("count", 0) or 0) < QZONE_DAILY_MAX
        except Exception:
            return True

    def _bump_qzone_budget(self) -> None:
        today = self._today()
        rec = self.state.get("qzone_budget")
        if not isinstance(rec, dict) or rec.get("date") != today:
            rec = {"date": today, "count": 0}
            self.state["qzone_budget"] = rec
        rec["count"] = int(rec.get("count", 0) or 0) + 1
        self._save_state()

    # ---------------- 睡衣 ----------------
    @staticmethod
    def _night_key(now: float) -> str:
        """一个「晚上」的标识：22:00~次日 07:00 算同一晚（凌晨算前一天）。"""
        local = local_datetime(now)
        return local_datetime(now - 86400 if local.hour < 7 else now).strftime('%Y-%m-%d')

    def _sleepwear_sets(self) -> list:
        """夜间睡衣库：世界设置 → 家里可增删改的列表；没配就用内置默认几套（含兜底那套）。"""
        raw = self._cfg("sleepwear", None)
        rows = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                desc = str(item.get("desc") or "").strip()
                name = str(item.get("name") or "").strip()
                if not desc:
                    continue
                rows.append({"name": name or desc[:20], "desc": desc})
        if rows:
            return rows
        return [dict(item) for item in SLEEPWEAR_SETS]

    def _night_sleepwear(self) -> dict:
        """今晚的睡衣：同一晚固定，换晚重新随机（且不会连续两晚一样）。"""
        key = self._night_key(self.clock.time())
        rec = self.state.get("night_sleepwear")
        if not isinstance(rec, dict):
            rec = {}
        pool_all = self._sleepwear_sets()
        if rec.get("night") == key and rec.get("name"):
            for item in pool_all:
                if item["name"] == rec["name"]:
                    return item
            # 拒图兜底写进来的保守款可能已经被用户从「夜间睡衣」里删掉：
            # 这一晚仍然用内置那套，保证重拍确实换成了最保守的衣服。
            if rec["name"] == SAFE_NIGHT_WEAR_NAME:
                for item in SLEEPWEAR_SETS:
                    if item["name"] == SAFE_NIGHT_WEAR_NAME:
                        return dict(item)
        last = str(rec.get("name") or "")
        pool_ = [s for s in pool_all if s["name"] != last] or pool_all
        picked = self.rng.choice(pool_)
        self.state["night_sleepwear"] = {"night": key, "name": picked["name"]}
        self._save_state()
        logger.info(f"[selfie] 今晚（{key}）的睡衣：{picked['name']}")
        return picked

    def _admins(self) -> list[str]:
        """谁可以用 admin 用法。

        插件配置里填了 admins_id 就只认这些号码；留空则沿用 AstrBot 全局 admins_id
        （这样不用为了一个插件去改全局管理员）。
        """
        local: list[str] = []
        try:
            raw_local = self.conf.get("admins_id") if hasattr(self.conf, "get") else None
            if isinstance(raw_local, (list, tuple)):
                local = [str(x).strip() for x in raw_local if str(x).strip().isdigit()]
            elif isinstance(raw_local, str) and raw_local.strip():
                local = [
                    x for x in re.split(r"[,，、\s]+", raw_local) if x.strip().isdigit()
                ]
        except Exception:
            local = []
        if local:
            return local
        try:
            cfg = self.context.get_config()
            raw = cfg.get("admins_id", []) if hasattr(cfg, "get") else []
        except Exception:
            raw = []
        return [str(x).strip() for x in raw if str(x).strip().isdigit()]

    @staticmethod
    def _clean_text(event: AstrMessageEvent) -> str:
        text = event.message_str or ""
        text = re.sub(r"\[At:\d+[^\]]*\]", "", text)
        text = re.sub(r"\[CQ:at[^\]]*\]", "", text)
        return text.strip()


    # ------------------------------------------------------------------
    # 天气 / 每天活动 / 场景
    # ------------------------------------------------------------------
    # ---------------- 行政区坐标：外部 districts.txt（热加载） ----------------


    # ---------------- 换装：场景/天气/节日/主人指令 -> 这套衣服 ----------------
    @staticmethod
    def _wear_text(look: dict) -> str:
        if look.get("full"):
            return str(look["full"])
        parts = [str(look.get(k) or "") for k in ("outer", "inner", "bottom", "extra")]
        return "、".join(p for p in parts if p)

    def _clothing_access(self, scene: dict, force: bool = False) -> bool:
        """统一的换装门控（自拍 / 特写 / 互动 / cos 共用这一套判断）。

        - 新 workspace 实例：只有「家里」（客厅/厨房/卧室等）或「在外住宿」
          （酒店/旅馆/青旅/寄宿）可以随时点单换衣；街上、学校、车站等地点不行。
        - 没有 workspace 的旧实例：保持「18:00-22:00 在家」的老规则，行为不变。
        - force：管理员旧能力，任意时间、任意地点都能换。
        - 事件阶段（scene 带 event_id / outfit）的服装排他，除 force 外不许点单打破。
        """
        try:
            legacy = not bool(self._workspace_enabled())
        except Exception:
            legacy = not bool(getattr(self, "_workspace", None))
        return can_change_clothes(scene, force=force, legacy=legacy)

    def _resolve_look(self, scene: dict, removal: dict | None = None,
                      month: int | None = None,
                      costume: dict | None = None,
                      force_costume: bool = False,
                      style: str | None = None,
                      ref_outfit: dict | None = None) -> dict:
        """解析这一张自拍穿什么。

        没有内置形象：默认整套衣服交给「参考图」页上传的参考图（ref_outfit=True），
        只有明确要换的场景（点单换装 / 睡衣 / 泳池 / 夜店 / 事件服装）才用提示词描述，
        并标 restyle=True 让提示词「不要照抄参考图里的衣服」。
        只要这张是换装（restyle），参考图里的帽子、发卡等头饰一律去掉，只保留发型发色。
        month 只为兼容旧测试保留；costume 是点单换装（能不能换由 _clothing_access 判定）；
        scene["outfit"] 是 living 引擎写的事件阶段服装，字符串或 dict 都行，
        优先于点单衣服，只有管理员 force 能盖过它。
        """
        scene = scene if isinstance(scene, dict) else {}

        def _strip_headwear(look_: dict) -> None:
            """换装：参考图里的帽子、发卡和头饰都要去掉，只保留参考图的发型和发色。"""
            look_["negative_extra"].extend(
                ["帽子", "帽檐", "发卡", "发箍", "头饰", "发绳"])
            look_["no_hat_clause"] = (
                "头发保持参考图里的发型和发色，但不要任何帽子、发卡、发箍、头饰和发绳")
            look_["edit_note"] += (
                "参考图里的帽子、发卡等头部装饰必须去掉：照片里不能出现任何帽子、"
                "发卡、发箍或头饰，只保留参考图里的发型和发色；")

        # scene["outfit"] 平时是 world 写回来的「当前这身」回显（字符串），只有事件场景
        # 才是 living 引擎给的阶段服装；回显不能再被当成「要重拍的新服装」。
        stage = event_outfit(scene)
        removal = removal if isinstance(removal, dict) else {}
        hat_off = bool(removal.get("hat_off"))
        coat_off = bool(removal.get("coat_off"))
        scene_type = str(scene.get("type") or "")
        sleep_slot = str(scene.get("slot") or "") == "sleep"
        is_event_scene = bool(scene.get("event") or scene.get("event_id"))
        is_night = scene_type in ("bed", "bed_sleepy") or (
            sleep_slot and not is_event_scene and not self._is_cybercafe_scene(scene)
        )
        look = {
            "hat": "", "hair": "", "necklace": False,
            "outer": "", "inner": "", "bottom": "", "extra": "", "full": "",
            "name": "", "caption": "", "negative_extra": [],
            "edit_note": "", "hat_off": hat_off, "coat_off": coat_off,
            "no_hat_clause": "头部保持参考图里的样子：帽子、发型和发色都不要改",
            # restyle=True 时提示词会额外要求「别照抄参考图的衣服，按这套重新拍」
            "restyle": False,
            # ref_outfit=True 时整套衣服交给参考图，不再叠服装提示词
            "ref_outfit": False,
            "ref_outfit_text": "",
        }
        # 这一张到底穿哪套：管理员 force > 事件阶段服装 > 能换衣的地点里的点单。
        # 事件阶段的服装压在睡衣前面，跨夜也不会被睡衣盖掉。
        forced = bool(costume and force_costume)
        chosen = costume if forced else (
            stage or (costume if (costume and self._clothing_access(scene)) else None))
        if chosen is not None:
            cname = str(chosen.get("name") or "换装")
            look.update({
                "hat": "", "necklace": False,
                "full": str(chosen.get("desc") or cname),
                "name": cname, "caption": cname,
                "negative_extra": ["帽子", "外套", "校服"],
                "restyle": True,
            })
            look["edit_note"] = (
                "参考图里的旧衣服不要照抄：这一张要把整套衣服换掉，"
                "上衣、下装（裙子或裤子）、鞋袜、配饰全部按下面这段描述替换；"
            )
            _strip_headwear(look)
        elif is_night:
            sleepwear_set = self._night_sleepwear()
            look.update({
                "hat": "", "necklace": False, "full": sleepwear_set["desc"],
                "name": sleepwear_set["name"], "caption": sleepwear_set["name"],
                "negative_extra": ["帽子", "外套", "校服"],
                "restyle": True,
            })
            _strip_headwear(look)
        elif scene_type == "pool":
            look.update({
                "hat": "", "necklace": False, "full": "保守款连体泳衣",
                "name": "连体泳衣", "caption": "泳衣",
                "negative_extra": ["帽子", "外套", "校服"],
                "restyle": True,
            })
            _strip_headwear(look)
        elif str(scene.get("event") or "") == "menclub":
            # 夜店：哥特/地雷/Y2K/视觉系，整晚固定（除非当场点菜换装）
            club = self._club_outfit()
            look.update({
                "hat": "", "necklace": False,
                "full": str(club["desc"]),
                "name": str(club["name"]), "caption": str(club["name"]),
                "negative_extra": ["帽子", "外套", "校服"],
                "restyle": True,
                "edit_note": (
                    "参考图里的旧衣服全部换掉；这一张按夜店造型重新拍："
                    "妆容浓、眼线重，整体偏暗黑，灯光是地下室那种激光和频闪；"
                ),
            })
            _strip_headwear(look)
        # 默认（没有换装）：整套衣服以参考图为准，提示词不再描述她的旧衣服
        if not look["restyle"]:
            look["ref_outfit"] = True
            look["ref_outfit_text"] = DEFAULT_REF_OUTFIT_TEXT
            look["name"] = DEFAULT_REF_OUTFIT_NAME
            look["caption"] = DEFAULT_REF_OUTFIT_CAPTION

        # 工作台条件参考图换了「主站参考图1（全身）」：这套衣服以参考图为准，
        # 换装、事件服装、睡衣这些 restyle 场合仍以提示词为准。
        try:
            condition_ref = self._ref_condition_body_active(scene, style)
        except Exception:
            condition_ref = False
        if condition_ref and not look["restyle"]:
            look.update({
                "hat": "", "outer": "", "inner": "", "bottom": "", "extra": "", "full": "",
                "necklace": False, "ref_outfit": True,
                "ref_outfit_text": CONDITION_REF_OUTFIT_TEXT,
                "name": CONDITION_REF_NAME, "caption": CONDITION_REF_CAPTION,
            })
        # 工作台「参考图」页给某张参考图起了衣服名字：测试面板选中它时整套衣服以那张
        # 参考图为准（不换装），帽/外套由 removal 的 hat_off / coat_off 去掉。
        forced_outfit = str((ref_outfit or {}).get("name") or "").strip() \
            if isinstance(ref_outfit, dict) else ""
        if forced_outfit:
            forced_outfit = forced_outfit[:40]
            look.update({
                "hat": "", "outer": "", "inner": "", "bottom": "", "extra": "", "full": "",
                "necklace": False, "restyle": False, "ref_outfit": True,
                "ref_outfit_text": "「" + forced_outfit + "」这一套（整套衣服以参考图为准）",
                "name": forced_outfit, "caption": forced_outfit,
            })
            if hat_off:
                look["negative_extra"].extend(["帽子", "帽檐", "发饰"])
                look["edit_note"] += (
                    "参考图里的帽子必须去掉：照片里不能出现任何帽子，头发按参考图；"
                )
        # 主人要求：摘掉帽子 / 脱掉外套 —— 这张自拍里不出现对应物件
        if hat_off:
            look["hat"] = ""
            look["negative_extra"].extend(["帽子", "帽檐", "发饰"])
            look["edit_note"] += (
                "参考图里的帽子必须去掉：照片里不能出现任何帽子，头发保持参考图里的样子；"
            )
        if coat_off and (look.get("outer") or look.get("ref_outfit")):
            look["outer"] = ""
            look["negative_extra"].extend(["外套", "羽绒服", "棉袄", "校服外套"])
            look["edit_note"] += (
                "参考图里的外套必须去掉：照片里不能出现外套，只保留里面的衣服；"
            )
        return look

    def _outfit_name(self, scene: dict) -> str:
        try:
            return str(self._resolve_look(scene).get("name") or "")
        except Exception:
            return ""

    def _home_lunch_meal(self) -> str:
        """今天在家做饭/吃午饭的那道菜（生活引擎写进 state['living']['days'][日期]['lunch']）。"""
        try:
            living = self.state.get("living") if isinstance(self.state, dict) else None
            if not isinstance(living, dict):
                return ""
            day = self.clock.strftime("%Y-%m-%d", self.clock.localtime())
            lunch = (living.get("days") or {}).get(day, {}).get("lunch") or {}
            return str(lunch.get("meal") or "").strip()
        except Exception:
            return ""

    def _home_room_pool(self) -> dict:
        """工作台「世界设置」里的房间（房间名 → 环境描述），拿不到就用内置的六间。"""
        try:
            rooms = self._workspace_settings().get("home_rooms")
        except Exception:
            rooms = None
        if isinstance(rooms, dict) and rooms:
            return rooms
        return dict(HOME_ROOMS)

    def _roll_home_room(self, scene: dict) -> dict:
        """在家拍照时每张重新掷一个房间：22:00~07:00 锁卧室，11:00~13:00 在厨房做饭/吃饭。

        场景本身仍由日程决定（在哪座城市、回没回家）；这里只换「这张照片在哪个房间」，
        所以同一个小时里连拍几张也不会全是客厅。
        """
        if not isinstance(scene, dict) or scene.get("event_id"):
            return scene
        if not (scene.get("home") or str(scene.get("type") or "") == "home_evening"):
            return scene
        rooms = self._home_room_pool()
        names = [name for name in HOME_ROOM_ORDER if name in rooms]
        names += [name for name in rooms if name not in HOME_ROOM_ORDER]
        if not names:
            return scene
        hour = int(self.clock.localtime().tm_hour)
        current = str(scene.get("room") or "")
        phase = str(scene.get("living_phase") or "")
        out = dict(scene)
        if hour >= 22 or hour < 7:
            room = "卧室" if "卧室" in rooms else names[0]
        elif 11 <= hour < 13:
            room = "厨房" if "厨房" in rooms else names[0]
            if not phase or phase in ("home", "return_home"):
                out["living_phase"] = "cook" if hour < 12 else "lunch"
                if not str(out.get("meal") or "").strip():
                    meal = self._home_lunch_meal()
                    if meal:
                        out["meal"] = meal
                if hour >= 12:
                    out["meal_table"] = True
        else:
            pool = [name for name in names if name != current] or names
            room = str(self.rng.choice(pool))
        out["room"] = room
        desc = rooms.get(room)
        if isinstance(desc, dict):
            desc = desc.get("desc")
        if isinstance(desc, str) and desc.strip():
            out["desc"] = desc
        if not str(out.get("activity") or "").strip():
            out["activity"] = f"在{room}里"
        return out

    # ---------------- 场景数据：外部 scenes.txt（热加载） ----------------


    # ---------------- 城市 / 交通 / 过夜 ----------------


    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------
    def _character(self, look: dict) -> str:
        """人物外观一律以参考图为准，这里只锁定「这次要不要换帽子/换衣服」。"""
        parts = ["成年女性", "长相、发型、发色严格以参考图为准，不要另外想象或改变"]
        if look.get("hat"):
            parts.append("戴" + str(look["hat"]))
        elif str(look.get("no_hat_clause") or "").strip():
            parts.append(str(look["no_hat_clause"]))
        parts.append("穿" + (self._ref_outfit_wear(look) if look.get("ref_outfit")
                             else self._wear_text(look)))
        return "，".join(parts)

    def _style(self, override: str | None = None) -> str:
        raw = str(override or self._cfg("style", STYLE_REALISTIC) or "").strip().lower()
        return STYLE_ALIASES.get(raw, STYLE_REALISTIC)

    def _detect_style_request(self, text: str) -> str | None:
        """从用户这句话里判断有没有点名风格（二次元 / 写实真人）。"""
        if not text:
            return None
        if REAL_STYLE_PAT.search(text):
            return STYLE_REALISTIC
        if ANIME_STYLE_PAT.search(text):
            return STYLE_ANIME
        return None

    def _detect_removal(self, text: str) -> dict:
        """识别主人这句话里的「摘掉帽子 / 脱掉外套」。

        返回 {"hat_off": bool, "coat_off": bool}；两个都没有就返回空 dict。
        只有管理员（主人）说了才生效，普通成员说这个不会改变衣服。
        """
        if not text:
            return {}
        removal = {}
        if HAT_OFF_PAT.search(text):
            removal["hat_off"] = True
        if COAT_OFF_PAT.search(text):
            removal["coat_off"] = True
        return removal

    # ---------------- 衣柜：外部 costumes.txt（热加载） ----------------
    def _material_dir(self) -> str:
        """素材库的运行时根目录：优先数据目录，没有就退回插件目录（老实例）。"""
        data_dir = getattr(self, "data_dir", "")
        return str(data_dir or getattr(self, "plugin_dir", ""))

    def _resolve_material_path(self, key: str, filename: str) -> str:
        """配置相对路径按数据目录解析；运行时文件缺失时退回插件目录的只读默认值。"""
        root = self._material_dir()
        raw = str(self._cfg(key, "") or "").strip()
        if raw:
            path = raw if os.path.isabs(raw) else os.path.join(root, raw)
            if os.path.exists(path):
                return path
            logger.warning(f"[selfie] {key} 指的 {path} 不存在，改用默认素材文件")
        for cand in (os.path.join(root, filename),
                     os.path.join(getattr(self, "plugin_dir", ""), filename)):
            if cand and os.path.exists(cand):
                return cand
        return ""

    def _costumes_path(self) -> str:
        return self._resolve_material_path("costume_file", COSTUME_FILENAME)

    def _meal_library(self) -> dict:
        """菜品素材库：菜名 → {name, description}，按 mtime 热加载。"""
        root = self._material_dir()
        path = os.path.join(root, FILENAMES["meal"])
        if not os.path.exists(path):
            path = os.path.join(getattr(self, "plugin_dir", ""), FILENAMES["meal"])
        try:
            mtime = os.path.getmtime(path)
            cache = getattr(self, "_meal_cache", None)
            if cache and cache[0] == path and cache[1] == mtime:
                return cache[2]
            with open(path, encoding="utf-8-sig") as fh:
                entries = parse_materials("meal", fh.read())
            library = {entry["name"]: entry for entry in entries}
            self._meal_cache = (path, mtime, library)
            return library
        except Exception:
            return {}

    def _selfie_actions(self) -> list:
        """自拍动作素材库：selfie_actions.txt（名字 | 表情 | 手势），按 mtime 热加载。

        文件缺失或解析失败时退回代码里的 SELFIE_ACTIONS，保证日常自拍永远有表情可用。
        """
        root = self._material_dir()
        path = os.path.join(root, FILENAMES["selfie"])
        if not os.path.exists(path):
            path = os.path.join(getattr(self, "plugin_dir", ""), FILENAMES["selfie"])
        try:
            mtime = os.path.getmtime(path)
            cache = getattr(self, "_selfie_action_cache", None)
            if cache and cache[0] == path and cache[1] == mtime:
                return cache[2]
            with open(path, encoding="utf-8-sig") as fh:
                entries = parse_materials("selfie", fh.read())
            actions = [entry for entry in entries
                       if str(entry.get("expression") or "").strip()]
            if not actions:
                raise RuntimeError("解析结果为空")
            self._selfie_action_cache = (path, mtime, actions)
            return actions
        except Exception:
            return [dict(item) for item in SELFIE_ACTIONS]

    def _costume_tables(self) -> tuple[dict, dict]:
        """读外部衣柜，返回 (名字->描述, 别名->名字)。

        每行：``名字 | 别名1,别名2 | 衣服描述``；# 开头是注释。
        按文件 mtime 缓存：改完 txt 保存后下一句对话就生效，不用重载插件。
        文件缺失 / 解析不出东西时，退回代码里的内置衣柜，保证不会没衣服可穿。
        """
        path = self._costumes_path()
        if not path:
            return COSTUME_OUTFITS, COSTUME_ALIASES
        try:
            mtime = os.path.getmtime(path)
            cache = getattr(self, "_costume_cache", None)
            if cache and cache[0] == path and cache[1] == mtime:
                return cache[2], cache[3]
            outfits: dict[str, str] = {}
            aliases: dict[str, str] = {}
            with open(path, encoding="utf-8-sig") as f:
                entries = parse_materials("costume", f.read())
            for entry in entries:
                key = entry["name"]
                outfits[key] = entry["description"]
                aliases.setdefault(key, key)
                for alias in entry["aliases"]:
                    aliases[alias] = key
            if not outfits:
                raise RuntimeError("解析结果为空")
            self._costume_cache = (path, mtime, outfits, aliases)
            logger.info(f"[selfie] 衣柜已加载 {len(outfits)} 套（{path}）")
            return outfits, aliases
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 读 costumes.txt 失败，改用内置衣柜: {e!r}")
            return COSTUME_OUTFITS, COSTUME_ALIASES

    def _detect_costume_regex(self, text: str) -> dict | None:
        """正则/别名兜底：识别「穿女仆装 / 换上护士服 / 来套婚纱」这类点菜换装。

        大模型不可用（没配 provider、超时、返回解析失败）时走这条路，
        保证换装功能不会因为模型抽风而整体失效。

        返回 {"name": 用户说的名字, "desc": 提示词用的服装描述}；没有就返回 None。
        """
        outfits, aliases = self._costume_tables()
        if not text:
            return None
        probe = text
        # 摘帽子/脱外套 是另一套逻辑，别被当成换装
        for pat in (HAT_OFF_PAT, COAT_OFF_PAT):
            probe = pat.sub("", probe)
        # 默认衣服不算换装
        for ex in _COSTUME_EXCLUDE:
            probe = probe.replace(ex, "")
        if not probe.strip():
            return None
        cand = ""
        m = _COSTUME_RE.search(probe)
        if m:
            cand = m.group(2)
        if not cand:
            for alias in sorted(aliases, key=len, reverse=True):
                if alias in probe:
                    cand = alias
                    break
        cand = str(cand or "").strip(" ·:：_-")
        if not cand:
            return None
        # 「那件粉色的洛丽塔裙」-> 抓里面的已知服装名「洛丽塔」
        key = None
        for alias in sorted(aliases, key=len, reverse=True):
            if alias in cand:
                cand = alias
                key = aliases[alias]
                break
        if not key:
            cand = re.sub(r"^(那件|这件|那条|这条|那个|这个|一件|一条|一套|一身|一个)", "", cand)
            key = aliases.get(cand)
        if not key:
            key = aliases.get(cand.lower())
        if not key:
            base = re.sub(r"(制服|套装|服装|cos服|婚纱|汉服|比基尼|死库水|礼服|服|装|裙|衣|袍|裤|袜|甲)$",
                          "", cand)
            key = aliases.get(base) or aliases.get(base.lower())
            cand_base = base or cand
        else:
            cand_base = cand
        if key and key in outfits:
            return {"name": cand, "desc": outfits[key]}
        # 没收录的「xx 服 / xx 衣 / xx 裙」按用户说法泛化，别扫兴
        if 2 <= len(cand_base) <= 10:
            return {
                "name": cand,
                "desc": (
                    f"{cand}：这种服装的典型款式，上装、下装、鞋袜和配饰都要拍全"
                    "（颜色、版型、材质、图案和配件细节清楚），是一整套完整的服装"
                ),
            }
        return None

    # ---------------- 点菜换装：大模型模糊匹配 ----------------
    def _costume_match_provider_id(self) -> str:
        """模糊匹配用的模型：优先插件里单独配的，留空就用当前对话模型。"""
        return str(self._cfg("costume_match_provider_id", "") or "").strip()

    def _wardrobe_for_match(self, with_desc: bool = False) -> str:
        """给匹配用的衣柜清单。

        默认只发「名字 | 别名」——判断该穿哪套只需要名字和别名，
        描述是给生图用的，发过去纯属浪费 token、拖慢响应。
        """
        outfits, aliases = self._costume_tables()
        rev: dict[str, list[str]] = {k: [] for k in outfits}
        for alias, key in aliases.items():
            if key in rev and alias != key:
                rev[key].append(alias)
        lines = []
        for name, desc in outfits.items():
            alias_txt = ",".join(sorted(rev.get(name) or [], key=len)[:8])
            if with_desc:
                lines.append(f"{name} | {alias_txt} | {desc}")
            else:
                lines.append(f"{name} | {alias_txt}" if alias_txt else name)
        return "\n".join(lines)

    @staticmethod
    def _clean_one_line(val: str, limit: int) -> str:
        """把模型输出的字段压成单行、去掉会破坏 costumes.txt 格式的字符。"""
        s = re.sub(r"[\r\n\t|]+", " ", str(val or ""))
        s = re.sub(r"\s+", " ", s).strip(" ·:：_-，,。")
        return s[:limit]

    def _append_costume_to_file(self, name: str, alias_field: str, desc: str) -> bool:
        """安全追加编号衣服；成功时保存本次确切行供学习回滚使用。"""
        self._last_appended_costume_line = ""
        path = self._costumes_path()
        if not path:
            logger.warning("[selfie] 找不到 costumes.txt，新衣服只在本条消息里生效")
            return False
        name = self._clean_one_line(name, 12)
        alias_field = self._clean_one_line(alias_field, 60)
        desc = self._clean_one_line(desc, 300)
        if not name or not desc:
            return False
        outfits, _ = self._costume_tables()
        if name in outfits:
            return False
        try:
            backup_dir = os.path.join(self.data_dir, "material_backups") if getattr(self, "data_dir", None) else None
            store = MaterialStore(self._material_dir(), {"costume": os.path.abspath(path)},
                                  backup_dir=backup_dir, fallback_root=self.plugin_dir)
            draft = store.create_draft("costume", f"{name} | {alias_field} | {desc}")
            entry = store.confirm_draft(draft["token"])
            self._last_appended_costume_line = f"{entry['id']}：{entry['text']}"
            self._costume_cache = None      # 下次读盘重新解析
            logger.info(f"[selfie] 衣柜新增一套：{name} —— {desc[:40]}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 写入 costumes.txt 失败: {e!r}")
            return False

    async def _llm_match_costume(self, text: str) -> dict | None:
        """让大模型把「用户那句话」模糊匹配到衣柜里的某一套。

        只发三样东西：用户指令、衣柜清单、判断规则。不发聊天历史、不发场景、不发图片。
        匹配上 → 用衣柜里那套的描述；一套都对不上 → 让模型现场写一套并追加进 costumes.txt
        （下次就是直接命中，不用再请模型写）。

        返回 {"name", "desc", "is_new"}；模型不可用/超时/解析失败返回 None（由调用方兜底）。
        """
        if not text or not text.strip():
            return None
        provider = None
        try:
            pid = self._costume_match_provider_id()
            if pid and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(pid)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return None
        with_desc = bool(self._cfg("costume_match_with_desc", False))
        wardrobe = self._wardrobe_for_match(with_desc=with_desc)
        prompt = (
            "你是服装衣柜检索器。用户想让角色换上一套衣服，"
            "你要判断用户说的是衣柜里的哪一套。\n\n"
            f"衣柜清单（格式：名字 | 别名）：\n{wardrobe}\n\n"
            f"判断规则：\n{COSTUME_MATCH_RULES}\n\n"
            '只输出 JSON，不要解释、不要代码块。三种格式三选一：\n'
            '{"dress": false}                                    ← 用户没在要求换衣服\n'
            '{"match": "衣柜里的名字"}                            ← 匹配到某套\n'
            '{"match": null, "new_name": "名字", "new_aliases": "别名1,别名2", '
            '"new_desc": "生图提示词"}                            ← 衣柜没有，新建一套\n\n'
            f"用户的话：{text[:200]}"
        )
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt),
                timeout=float(self._cfg("costume_match_timeout", 12) or 12),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 换装模糊匹配调用失败，改用正则兜底: {e!r}")
            return None
        raw = (getattr(resp, "completion_text", "") or "").strip()
        if not raw:
            return None
        data = self._parse_costume_match_json(raw)
        if not isinstance(data, dict):
            logger.warning(f"[selfie] 换装匹配返回无法解析，改走兜底: {raw[:80]!r}")
            return None
        outfits, aliases = self._costume_tables()
        if data.get("dress") is False or str(data.get("dress")).lower() == "false":
            return None                  # 模型判定：这句话不是在点衣服
        matched = self._clean_one_line(data.get("match") or "", 20)
        # 模型偶尔会返回别名而不是正名，这里再兜一层
        key = matched if matched in outfits else aliases.get(matched) or aliases.get(matched.lower())
        if key and key in outfits:
            logger.info(f"[selfie] 换装模糊匹配：「{text[:20]}」→ {key}")
            return {"name": key, "desc": outfits[key], "is_new": False}
        new_name = self._clean_one_line(data.get("new_name") or "", 12)
        new_desc = self._clean_one_line(data.get("new_desc") or "", 300)
        if not new_name or len(new_desc) < 12:
            logger.info(f"[selfie] 换装模糊匹配：模型判为无匹配且没给出可用新衣服（{matched!r}）")
            return None
        alias_field = self._clean_one_line(data.get("new_aliases") or "", 60)
        self._append_costume_to_file(new_name, alias_field, new_desc)
        logger.info(f"[selfie] 换装模糊匹配：衣柜没有，新建「{new_name}」")
        return {"name": new_name, "desc": new_desc, "is_new": True}

    @staticmethod
    def _parse_costume_match_json(raw: str) -> dict | None:
        """从模型回复里抠出 JSON（容忍 ```json 包裹、前后废话）。"""
        s = str(raw or "").strip()
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        try:
            return json.loads(s)
        except Exception:
            pass
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    def _costume_request_gate(self, text: str) -> bool:
        """廉价预筛：这句话到底有没有可能在点衣服。

        纯「来张特写」「拍张自拍」这种一句都不沾衣服的，直接跳过，
        不浪费一次模型调用（普通特写是最常见的请求，必须快）。
        宁可放宽一点，别把真·点菜漏掉。
        """
        if not text:
            return False
        probe = text
        for pat in (HAT_OFF_PAT, COAT_OFF_PAT):
            probe = pat.sub("", probe)
        for ex in _COSTUME_EXCLUDE:
            probe = probe.replace(ex, "")
        if not probe.strip():
            return False
        if _COSTUME_HINT_PAT.search(probe):
            return True
        _, aliases = self._costume_tables()
        return any(alias in probe for alias in aliases)

    async def _detect_costume(self, text: str, llm: bool = True) -> dict | None:
        """点菜换装的总入口：先让大模型模糊匹配，失败再退回正则/别名。

        llm=False：这个时段反正换不了装（马上要回婉拒），只要知道「有没有在点衣服」，
        不花模型调用，直接用正则判。
        """
        if not text:
            return None
        if llm and bool(self._cfg("costume_match_llm", True)):
            if not self._costume_request_gate(text):
                return None          # 不像在点衣服，别惊动模型
            got = await self._llm_match_costume(text)
            if got:
                return got
        return self._detect_costume_regex(text)

    def _costume_confirm_text(self, costume: dict) -> str:
        """「我去找一件xxx衣服，你要的是这个对吧」——换装确认语。

        名字本身已经是服装名词（护士服 / 洛丽塔裙 / 冬日毛呢大衣）就直接说「一件xxx」；
        只是身份/角色（护士、兔女郎）就说「一件xxx的衣服」，别出现「一件护士」这种病句。
        """
        name = str(costume.get("name") or "").strip() or "衣服"
        if name[-1] in COSTUME_TAIL_CHARS:
            return COSTUME_CONFIRM.format(name=name)
        return COSTUME_CONFIRM.format(name=f"{name}的衣服")

    # ------------------------------------------------------------------
    # cosplay：先搜角色图 -> 请对方确认 -> 双图生成
    # ------------------------------------------------------------------
    def _detect_cos(self, text: str) -> str:
        """识别「cos 初音未来」「cosplay 雷姆」「扮演明日香」里的角色名。"""
        if not text:
            return ""
        m = COS_PAT.search(text)
        if not m:
            return ""
        name = str(m.group(1) or "").strip()
        # 去掉后面跟的「自拍/照片/吧」这类词
        # 「cos 自拍一张」这种没有名字，直接返回空
        if any(name.startswith(stop) for stop in COS_STOPWORDS):
            return ""
        # 在第一个「自拍 / 拍一张 / 照片」这类词处截断，剩下的才是角色名
        cuts = [i for i in (name.find(stop) for stop in COS_STOPWORDS) if i > 0]
        if cuts:
            name = name[: min(cuts)]
        # 去掉语气词和标点
        name = re.sub(r"[吧呢啊嘛呀哦的了~\s。!?，,]+$", "", name)
        name = re.sub(r"\s+", " ", name).strip(" ·:：_-，,。.！!？?")
        if any(k in name for k in ("什么", "是谁", "怎么", "为什么", "吗", "？", "?")):
            return ""
        if len(name) < 2 or len(name) > 20:
            if len(name) == 0:
                return ""
        return name

    # ---------------- 风格偏好（按 QQ 号记，默认真人版） ----------------
    def _user_style(self, sender: str) -> str:
        prefs = self.state.get("style_prefs")
        if isinstance(prefs, dict):
            val = prefs.get(str(sender))
            if val in (STYLE_ANIME, STYLE_REALISTIC):
                return val
        return ""

    def _set_user_style(self, sender: str, style: str) -> None:
        sender = str(sender or "").strip()
        if not sender or style not in (STYLE_ANIME, STYLE_REALISTIC):
            return
        prefs = self.state.get("style_prefs")
        if not isinstance(prefs, dict):
            prefs = {}
        prefs[sender] = style
        self.state["style_prefs"] = prefs
        self._save_state()

    def _detect_style_pref(self, text: str) -> tuple[str, str]:
        """识别「自拍风格改为二次元 / 以后都用写实」这类长期偏好设置。

        返回 (style 或空串, 去掉设置语后的剩余文本)。
        """
        if not text:
            return "", text
        m = STYLE_PREF_PAT.search(text) or STYLE_PREF_PAT2.search(text)
        if not m:
            return "", text
        word = str(m.group(1) or "").strip()
        style = STYLE_ALIASES.get(word.lower()) or STYLE_ALIASES.get(word)
        if not style:
            for alias, st in STYLE_ALIASES.items():
                if alias in word.lower():
                    style = st
                    break
        if not style:
            return "", text
        rest = (text[: m.start()] + text[m.end():]).strip(" 　，,。.！!～~、")
        return style, rest

    def _can_trigger(self, event, text: str) -> bool:
        """Private chat, explicit wake-up, or the configured character's name."""
        try:
            if event.is_private_chat():
                return True
        except Exception:
            return True
        try:
            if event.is_at_or_wake_command:
                return True
        except Exception:
            pass
        profile = self._profile()
        return any(name and name in (text or "") for name in (profile.name,))

    def _cos_refusal(self, slot: str = "", scene: dict | None = None) -> str:
        """不能换衣服时点 cos 的婉拒话术；能换返回空串。

        传 scene 时按「家/住宿」地点规则判断（没有 workspace 的旧实例仍是 18-22 在家）；
        不传 scene 时退回旧的按时段判断。
        """
        if isinstance(scene, dict):
            if self._clothing_access(scene):
                return ""
            if event_outfit(scene) is not None:
                return "现在这身是活动安排好的，cos 就先不换了，等结束再说～"
            slot = str(scene.get("slot") or "")
        if slot == "sleep":
            return "现在太晚啦，我要睡了，cos 还得换衣服呢，明天再说吧～"
        return "白天在外面呢，换衣服 cos 不太方便，等我回住处再说～"

    def _is_cos_selfie(self, text: str) -> bool:
        """同时出现「cos」和自拍请求，才进 cosplay 模式。"""
        return bool(text) and bool(COS_KEYWORD_PAT.search(text)) and bool(self._selfie_match(text))

    @staticmethod
    def _detect_scene_request(text: str) -> bool:
        """是不是在要求她换地方拍（她的活动自己安排，不会被点单）。"""
        return bool(text) and bool(SCENE_REQ_PAT.search(text))

    def _costume_refusal(self, slot: str = "", scene: dict | None = None) -> str:
        """不能点单换装时的婉拒话术；能换返回空串。

        传 scene 时按「家/住宿」地点规则判断（没有 workspace 的旧实例仍是 18-22 在家）；
        不传 scene 时退回旧的按时段判断。事件阶段那套衣服是活动定的，点单换不掉。
        """
        if isinstance(scene, dict):
            if self._clothing_access(scene):
                return ""
            if event_outfit(scene) is not None:
                return "现在这身是活动安排好的，先不换啦，等结束再说～"
            slot = str(scene.get("slot") or "")
        if slot == "home_evening":
            return ""
        if slot == "sleep":
            return "现在太晚啦，我要睡了，明天再说吧～"
        return "白天在外面呢，换衣服不太方便，等我回住处再说～"

    # ---------------- 特写：提示词 txt / 每次随机抽一条 ----------------
    @staticmethod
    def _scene_hard_note(scene: dict) -> str:
        """某些地点的硬性要求（如大明宫必须是金黄色墙），追加到提示词末尾。"""
        if not isinstance(scene, dict):
            return ""
        blob = f"{scene.get('desc') or ''}{scene.get('activity') or ''}"
        return "".join(note for key, note in SCENE_HARD_NOTES if key and key in blob)

    def _closeup_prompt_path(self) -> str:
        """特写提示词 txt 路径：配置 > 数据目录 > 插件目录里的默认文件。

        配置里写的路径如果不存在，不再直接返回它（那样会打开失败、悄悄掉回内置 8 条），
        而是记一条 WARNING 继续往后找，保证「文件被挪走/删掉」时还有别的备份顶上。
        """
        root = self._material_dir()
        raw = str(self._cfg("closeup_prompt_file", "") or "").strip()
        if raw:
            p = raw if os.path.isabs(raw) else os.path.join(root, raw)
            if os.path.exists(p):
                return p
            logger.warning(f"[selfie] closeup_prompt_file 指的 {p} 不存在，继续找数据目录 / 插件目录")
        for cand in (
            os.path.join(root, "closeup_prompts.txt"),
            os.path.join(self.plugin_dir, "closeup_prompts.txt"),
            *CLOSEUP_PROMPT_FILE_CANDIDATES,
        ):
            if cand and os.path.exists(cand):
                return cand
        return ""

    def _closeup_prompts(self) -> list:
        """读出特写提示词列表：每行一条，返回 [{"id", "text"}]。

        id 是稳定的内部素材编号；对外显示号由当前文字顺序和线稿顺序计算
        已存在但为空的文件表示用户删空了文字库，不重新塞入内置动作
        """
        path = self._closeup_prompt_path()
        if path:
            try:
                mtime = os.path.getmtime(path)
                cache = getattr(self, "_closeup_cache", None)
                if cache and cache[0] == path and cache[1] == mtime:
                    return list(cache[2])
                with open(path, encoding="utf-8-sig") as f:
                    entries = parse_materials("closeup", f.read())
                self._closeup_cache = (path, mtime, entries)
                logger.info(f"[selfie] 特写提示词已加载 {len(entries)} 条：{path}")
                return list(entries)
            except Exception as e:
                logger.warning(f"[selfie] 读取特写提示词失败，改用内置 8 条: {e!r}")
        logger.warning(
            "[selfie] 找不到特写提示词文件（配置和插件目录都没有），"
            "临时用内置 8 条兜底 —— 请检查 closeup_prompt_file"
        )
        return [{"id": index, "text": text}
                for index, text in enumerate(CLOSEUP_PROMPTS_FALLBACK, 1)]

    def _pose_sketch_library(self):
        """动作线稿与人物参考图库分开保存，客户端只能传编号"""
        try:
            from .selfie_core.pose_sketch import PoseSketchLibrary
        except ImportError:
            from selfie_core.pose_sketch import PoseSketchLibrary
        root = os.path.join(self.data_dir, "pose_sketches")
        library = getattr(self, "_pose_sketch_store", None)
        if library is None or str(library.root) != str(root):
            library = self._pose_sketch_store = PoseSketchLibrary(root)
        return library

    def _pose_sketch_rows(self) -> list:
        # 未配置本地数据目录的旧测试桩不访问磁盘
        if not getattr(self, "data_dir", ""):
            return []
        return self._pose_sketch_library().rows()

    def _closeup_pm_ids(self) -> set:
        """需要 pm 才能抽到的提示词编号集合（按 txt 行号，从 1 开始）。

        这里不走 _cfg()：_cfg 会把空串当成「没配置」回落到默认 2,6，
        而用户把 closeup_pm_ids 清空的意思恰恰是「全部变普通」，所以直接读 conf。
        """
        try:
            raw = str(self.conf.get("closeup_pm_ids", DEFAULT_CLOSEUP_PM_IDS) or "").strip()
        except Exception:
            raw = DEFAULT_CLOSEUP_PM_IDS
        out = set()
        for part in re.split(r"[,，、;；\s]+", raw):
            try:
                n = int(part)
            except Exception:
                continue
            if n > 0:
                out.add(n)
        return out

    def _pick_closeup_prompt(self, pm: bool = False,
                             forced_id: int | None = None,
                             allow_sketch: bool = True) -> dict:
        """抽一条特写提示词（不会连着两次抽到同一条）。

        - pm=False（普通「特写」）：只抽普通池，排除 closeup_pm_ids 里那几条
        - pm=True （「特写pm」）  ：只抽 closeup_pm_ids 里那几条（默认第 2、6 条）
        - forced_id（管理员「admin特写N」）：直接取第 N 条；越界返回 {"error": "out_of_range"}
        对外编号为当前文字列表 1..N，再接线稿 N+1..M，删除后动态前移；
        内部 material_id / sketch_id 保持不变，pm 编号按当前文字显示号判定。
        """
        pool = self._closeup_prompts()
        # 对外编号动态连续：文字特写在前，线稿在后；内部素材 ID 不重写
        pm_ids = self._closeup_pm_ids()
        entries = [{"id": number, "material_id": int(entry["id"]),
                    "text": str(entry["text"]),
                    "tier": "special" if number in pm_ids else "normal"}
                   for number, entry in enumerate(pool, 1)]
        sketches = self._pose_sketch_rows() if allow_sketch and (not pm or forced_id) else []
        for ordinal, row in enumerate(sketches, 1):
            entries.append({"id": len(pool) + ordinal, "sketch_id": row["id"],
                            "text": f"线稿参考特写：{row.get('label') or '动作线稿'}",
                            "tier": "sketch"})
        if not entries:
            return {"error": "out_of_range", "id": forced_id or 0, "total": 0, "text": ""}
        if forced_id:
            try:
                want = int(forced_id)
            except Exception:
                want = 0
            if not 1 <= want <= len(entries):
                return {"error": "out_of_range", "id": want, "total": len(entries), "text": ""}
            pick = dict(entries[want - 1], forced=True)
        else:
            normal = [e for e in entries if e["tier"] != "special"]
            special = [e for e in entries if e["tier"] == "special"]
            cand = (special or entries) if pm else (normal or entries)
            pick = self.rng.choice(cand)
            last_key = self.state.get("closeup_last_key")
            def identity(entry):
                return (f"sketch:{entry['sketch_id']}" if "sketch_id" in entry
                        else f"text:{entry['material_id']}")
            others = [e for e in cand if (identity(e) != last_key if last_key else
                                         e["text"] != self.state.get("closeup_last"))]
            if len(cand) > 1 and others and pick not in others:
                pick = self.rng.choice(others)
        result = dict(pick, total=len(entries), pm=bool(pm))
        self.state["closeup_last"] = result["text"]
        self.state["closeup_last_key"] = (f"sketch:{result['sketch_id']}" if "sketch_id" in result
                                          else f"text:{result['material_id']}")
        self.state["closeup_last_id"] = result["id"]
        self.state["closeup_last_pm"] = bool(pm)
        self._save_state()
        logger.info(f"[selfie] 特写{'指定' if forced_id else '抽到'}编号 {result['id']}"
                    f"（共 {len(entries)} 条，{result['tier']}）：{result['text'][:40]}")
        return result

    async def _pick_closeup_cue(self, event, pm: bool, forced_id,
                               allow_sketch: bool = True) -> dict | None:
        """管理员指定了编号就按编号来；编号越界回一句范围并返回 None。"""
        cue = (self._pick_closeup_prompt(pm, forced_id) if allow_sketch else
               self._pick_closeup_prompt(pm, forced_id, allow_sketch=False))
        if cue.get("error") == "out_of_range":
            await event.send(MessageChain([Plain(
                f"编号 {cue['id']} 没有哦，现在一共 {cue['total']} 条，换一个编号？"
            )]))
            return None
        return cue

    @staticmethod
    def _clean_closeup_cue(text) -> str:
        """把 txt 里抽到的那条要求里「背景 / 风格 / 画幅比例」这类小句剔掉。

        例：「服装不变，背景环境为傍晚的室内少女卧室，人物慵懒地靠在椅子上」
        ->  「服装不变，人物慵懒地靠在椅子上」
        背景统一由 _get_scene() 的真实场景来写，风格/画幅由配置决定。
        """
        raw = str(text or "").strip()
        if not raw:
            return ""
        parts = re.split(r"([，。；、,;])", raw)
        out = []
        for i in range(0, len(parts), 2):
            seg = parts[i]
            sep = parts[i + 1] if (i + 1) < len(parts) else ""
            if seg.strip() and CLOSEUP_CUE_DROP_PAT.search(seg):
                continue
            out.append(seg + sep)
        cleaned = "".join(out)
        # 风格词就地删（保留同小句里的动作/构图），例如「手绘风格」「采用动漫处理厚涂风插画特写方式」
        cleaned = CLOSEUP_CUE_STYLE_PAT.sub("", cleaned)
        # 顺手修错字
        for bad, good in CLOSEUP_CUE_FIXUPS:
            cleaned = cleaned.replace(bad, good)
        # 过审：敏感词换成过审说法，原动作不失真（词表见 CLOSEUP_CUE_SOFTEN）
        hits = [bad for bad, _good in CLOSEUP_CUE_SOFTEN if bad in cleaned]
        if hits:
            for bad, good in CLOSEUP_CUE_SOFTEN:
                cleaned = cleaned.replace(bad, good)
            logger.info(f"[selfie] 特写要求里的敏感词已换成过审说法：{','.join(hits)}")
        cleaned = re.sub(r"([，。；、,;])\1+", r"\1", cleaned)
        cleaned = re.sub(r"[，。；、,;]{2,}", "，", cleaned)
        return cleaned.strip(" ，。；、,;")

    # ---------------- 互动：摸摸 / 捏捏 / 戳戳 / 揉揉 / 拍拍 的提示词 ----------------
    def _interact_prompt_path(self) -> str:
        """配置路径优先，其次数据目录，最后插件目录（兼容旧中文文件名）。"""
        root = self._material_dir()
        raw = str(self._cfg("interact_prompt_file", "") or "").strip()
        if raw:
            p = raw if os.path.isabs(raw) else os.path.join(root, raw)
            if os.path.exists(p):
                return p
            logger.warning(f"[selfie] interact_prompt_file 指的 {p} 不存在，改用默认素材文件")
        for base in (root, getattr(self, "plugin_dir", "")):
            for filename in (INTERACT_PROMPT_FILENAME,):
                cand = os.path.join(base, filename)
                if os.path.exists(cand):
                    return cand
        return ""

    def _interact_prompts(self) -> list:
        """读互动提示词，返回 [{"action", "text", "id"}]。

        每行一条，行首编号自动去掉；行首「动作词：」决定这条归哪个触发词
        （摸摸 / 捏捏 / 戳戳 / 揉揉 / 拍拍……），后面的正文才是生图提示词。
        文件改了会自动重读（按 mtime 判断），和特写提示词一个套路。
        """
        path = self._interact_prompt_path()
        if not path:
            return []
        try:
            mtime = os.path.getmtime(path)
            cache = getattr(self, "_interact_cache", None)
            if cache and cache[0] == path and cache[1] == mtime:
                return [dict(x) for x in cache[2]]
            with open(path, encoding="utf-8-sig") as f:
                raw_text = f.read()
            out = []
            for entry in parse_materials("interaction", raw_text):
                action = str(entry.get("action") or "").strip()
                text = str(entry.get("text") or "").strip()
                if action and text.startswith(action):
                    text = re.sub(r"^" + re.escape(action) + r"\s*[：:｜|]\s*",
                                  "", text).strip()
                if not text:
                    continue
                out.append({"action": action, "text": text,
                            "id": int(entry.get("id") or 0)})
            if out:
                self._interact_cache = (path, mtime, out)
                logger.info(f"[selfie] 互动提示词已加载 {len(out)} 条：{path}")
                return [dict(x) for x in out]
            logger.warning(f"[selfie] 互动提示词文件是空的：{path}")
        except Exception as e:
            logger.warning(f"[selfie] 读取互动提示词失败: {e!r}")
        return []

    def _interact_triggers(self) -> tuple:
        """可触发的互动词：内置表 + 互动库行首的动作词（库加新词就自动生效）。"""
        words = list(INTERACT_TRIGGERS)
        for entry in self._interact_prompts():
            action = str(entry.get("action") or "").strip()
            if action and action not in words:
                words.append(action)
        return tuple(words)

    def _interact_has_prompt(self, action: str) -> bool:
        """互动库里有没有以这个动作词开头的条目。"""
        return any(entry.get("action") == action for entry in self._interact_prompts())

    def _pick_interact_prompt(self, action: str,
                              forced_id: int | None = None) -> dict:
        """按触发词在互动库里 roll 一条提示词（不会连着两次抽到同一条）。

        - 库里没有以这个动作词开头的条目：返回 error="no_prompt"，由调用方回害羞的话
        - 有两条以上：在它们之间随机 roll 一条
        - forced_id（管理员「admin摸摸2」）：直接取素材编号为 2 的那一条
        """
        pool = self._interact_prompts()
        if not pool:
            return {"error": "no_file", "id": 0, "total": 0, "text": ""}
        if forced_id:
            try:
                want = int(forced_id)
            except Exception:
                want = 0
            pick = next((e for e in pool if int(e.get("id") or 0) == want), None)
            if want < 1 or pick is None:
                return {"error": "out_of_range", "id": want, "total": len(pool), "text": ""}
            idx = want
        else:
            cand = [e for e in pool if e.get("action") == action]
            if not cand:
                return {"error": "no_prompt", "id": 0, "total": len(pool), "text": ""}
            last = str(self.state.get("interact_last") or "")
            fresh = [e for e in cand if e.get("text") != last]
            pick = self.rng.choice(fresh or cand)
            idx = int(pick.get("id") or 0)
        self.state["interact_last"] = pick.get("text") or ""
        self.state["interact_last_action"] = action
        self._save_state()
        logger.info(
            f"[selfie] 互动提示词（{action}）抽到编号 {idx}/{len(pool)}："
            f"{str(pick.get('text'))[:40]}"
        )
        return {
            "id": idx, "total": len(pool), "text": pick.get("text") or "",
            "tier": action, "kind": action,
        }

    async def _interact_ack(self, event, action: str) -> str:
        """接单话：因地制宜地答应让她摸摸 / 戳戳。

        拟人模式：像聊天一样回应，第一句末尾（咔嚓）；关掉则旧短句 + 🤳。
        """
        pool = INTERACT_ACK_FALLBACK.get(action) or [
            f"好呀，我这就让你{action}", "来嘛，轻一点哦", f"嘻嘻，{action}一下就好",
        ]
        if self._natural_chat_enabled():
            return await self._natural_ack(
                event,
                action_line=f"让对方{action}你一下，同时拍一张近距离特写",
                fallback_pool=pool,
            )
        text = ""
        if bool(self._cfg("ack_llm", True)):
            try:
                provider = self.context.get_using_provider()
                if provider is None:
                    raise RuntimeError("没有可用的对话模型")
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        system_prompt=(
                            "你是这个账号里的角色本人，用第一人称说话，"
                            "自然、口语、简短，不油腻。"
                        ),
                        prompt=(
                            f"对方要你让他{action}一下，还要一张你被{action}时的近距离照片。"
                            f"用一句 8~20 字的第一人称中文答应下来，顺着他的话，"
                            f"像「好呀我这就让你{action}」这种口气；"
                            "只输出这一句话，不要引号、不要emoji、不要解释。"
                        ),
                    ),
                    timeout=10,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
                text = text.strip("“”\"'` \n")
                text = re.sub(r"[🤳📸✨😀-🙏\U0001F300-\U0001FAFF]+", "", text).strip()
                text = text.strip("。！!～~ ，,")
                if not (2 <= len(text) <= 40):
                    text = ""
            except Exception as e:
                logger.warning(f"[selfie] 互动确认语生成失败，用内置短句: {e!r}")
        if not text:
            text = self.rng.choice(pool)
        if action and action not in text:
            text = f"好呀，我这就让你{action}"
        return text.rstrip("。！!～~ ") + "🤳"

    async def _interact_cue_error(self, event, cue: dict) -> bool:
        """互动提示词抽不到时的回话（库里没这个词 / 文件缺失 / 管理员编号越界）。"""
        if cue.get("error") == "no_prompt":
            # 触发词在库里没有对应提示词：害羞不接单，不出图
            await event.send(MessageChain([Plain(self.rng.choice(INTERACT_SHY_FALLBACK))]))
        elif cue.get("error") == "out_of_range":
            await event.send(MessageChain([Plain(
                f"编号 {cue['id']} 没有哦，现在一共 {cue['total']} 条，换一个编号？"
            )]))
        else:
            await event.send(MessageChain([Plain(INTERACT_NO_FILE_TEXT)]))
        return True










    @staticmethod
    def _closeup_ack(costume: bool = False) -> str:
        pool_ = COSTUME_CLOSEUP_ACK_FALLBACK if costume else CLOSEUP_ACK_FALLBACK
        return random.choice(pool_)

    def _cos_dir(self) -> str:
        d = os.path.join(self.data_dir, "cos")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    @staticmethod
    def _requests_json(url: str, *, method: str = "GET", json_body: dict | None = None,
                       params: dict | None = None, headers: dict | None = None,
                       timeout: int = 20):
        """同步 requests 一层封装，交给 asyncio.to_thread 跑。

        aiohttp 连 api.bgm.tv 会一直卡在 connect 阶段（requests 正常），所以这里统一用 requests。
        """
        import requests
        h = {"User-Agent": BANGUMI_UA}
        if headers:
            h.update(headers)
        if method.upper() == "POST":
            r = requests.post(url, json=json_body, params=params, headers=h, timeout=timeout)
        else:
            r = requests.get(url, params=params, headers=h, timeout=timeout)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.json()

    async def _download_image(self, url: str) -> bytes | None:
        try:
            data = await download_public_image(url, timeout_seconds=30, max_bytes=8 * 1024 * 1024)
        except Exception as e:
            logger.info(f"[selfie] 下载角色图失败: {e!r}")
            return None
        if not data or len(data) > 8 * 1024 * 1024:
            return None
        return data

    def _store_cos_image(self, data: bytes, char_name: str) -> str | None:
        """把角色图转成不超过 800px 的 PNG 存下来，返回本地路径。"""
        try:
            from PIL import Image as PILImage
            import io
            with PILImage.open(io.BytesIO(data)) as im:
                im = im.convert("RGB")
                w, h = im.size
                if max(w, h) > COS_IMAGE_MAX:
                    scale = COS_IMAGE_MAX / float(max(w, h))
                    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), PILImage.LANCZOS)
                safe = re.sub(r"[^\w\u4e00-\u9fa5-]+", "_", char_name)[:30] or "char"
                path = os.path.join(
                    self._cos_dir(),
                    f"cos_{safe}_{self.clock.strftime('%Y%m%d_%H%M%S')}_{self.rng.randint(100, 999)}.png",
                )
                im.save(path, "PNG")
                return path
        except Exception as e:
            logger.warning(f"[selfie] 角色图转存失败: {e!r}")
            return None

    async def _search_bangumi(self, name: str) -> list[dict]:
        out = []
        try:
            data = await asyncio.to_thread(
                self._requests_json,
                BANGUMI_CHAR_API,
                method="POST",
                json_body={"keyword": name, "filter": {"nsfw": False}},
                headers={"User-Agent": BANGUMI_UA},
            )
        except Exception as e:
            logger.info(f"[selfie] Bangumi 搜索失败: {e!r}")
            return out
        for item in (data.get("data") or [])[: COS_MAX_CANDIDATES * 2]:
            if not isinstance(item, dict):
                continue
            images = item.get("images") or {}
            url = images.get("large") or images.get("medium") or images.get("small")
            if not url:
                continue
            out.append({
                "name": str(item.get("name") or name),
                "url": str(url),
                "source": "bangumi",
            })
        # 名字完全对上的排前面
        out.sort(key=lambda x: (x["name"] != name, name not in x["name"]))
        return out[:COS_MAX_CANDIDATES]

    async def _search_moegirl(self, name: str) -> list[dict]:
        out = []
        ua = {"User-Agent": BROWSER_UA}
        try:
            data = await asyncio.to_thread(
                self._requests_json,
                MOEGIRL_API,
                params={
                    "action": "query", "titles": name, "prop": "pageimages",
                    "format": "json", "pithumbsize": str(COS_IMAGE_MAX), "redirects": "1",
                },
                headers=ua,
            )
            for page in ((data.get("query") or {}).get("pages") or {}).values():
                thumb = (page.get("thumbnail") or {}).get("source")
                if thumb:
                    out.append({
                        "name": str(page.get("title") or name),
                        "url": str(thumb),
                        "source": "moegirl",
                    })
        except Exception as e:
            logger.info(f"[selfie] 萌娘百科词条图失败: {e!r}")
        if not out:
            try:
                data = await asyncio.to_thread(
                    self._requests_json,
                    MOEGIRL_API,
                    params={
                        "action": "query", "generator": "search", "gsrsearch": name,
                        "gsrlimit": "5", "prop": "pageimages", "piprop": "thumbnail",
                        "pithumbsize": str(COS_IMAGE_MAX), "format": "json",
                    },
                    headers=ua,
                )
                pages = ((data.get("query") or {}).get("pages") or {})
                items = sorted(pages.values(), key=lambda x: x.get("index") or 99)
                for page in items:
                    title = str(page.get("title") or "")
                    thumb = (page.get("thumbnail") or {}).get("source")
                    if not thumb:
                        continue
                    if name not in title and out:
                        continue
                    out.append({"name": title or name, "url": str(thumb), "source": "moegirl"})
                    if len(out) >= 2:
                        break
            except Exception as e:
                logger.info(f"[selfie] 萌娘百科搜索兜底失败: {e!r}")
        return out[:COS_MAX_CANDIDATES]

    async def _search_character_images(self, name: str) -> list[dict]:
        """搜角色图：Bangumi 优先，萌娘百科兜底；返回带本地路径的候选列表。"""
        raw = await self._search_bangumi(name)
        if not raw:
            raw = await self._search_moegirl(name)
        results = []
        for item in raw:
            data = await self._download_image(item["url"])
            if not data:
                continue
            path = self._store_cos_image(data, item["name"] or name)
            if not path:
                continue
            item = dict(item)
            item["path"] = path
            results.append(item)
            if len(results) >= COS_MAX_CANDIDATES:
                break
        logger.info(f"[selfie] 角色「{name}」搜到 {len(results)} 张候选图")
        return results

    def _prune_cos_files(self) -> None:
        try:
            cutoff = self.clock.time() - 86400
            for name in os.listdir(self._cos_dir()):
                p = os.path.join(self._cos_dir(), name)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass


    # ------------------------------------------------------------------
    # cosplay：搜图 -> 请对方确认 -> 「我要 cos X 啦」 -> 双图生成
    # ------------------------------------------------------------------
    def _caption_provider_id(self) -> str:
        try:
            cfg = self.context.get_config()
            ps = cfg.get("provider_settings") or {}
            return str(ps.get("default_image_caption_provider_id") or "")
        except Exception:
            return ""

    async def _describe_character_costume(self, path: str, cos_name: str,
                                          include_hair: bool = False) -> str:
        """用支持视觉的模型描述角色身上的服装（不要发色发型），双图失败时兜底用。

        include_hair=True（cos 连头发一起换）：把发色、发型、头发长度也写进描述，
        单图站点没有角色图可参考时，靠这段文字复刻角色的头发。
        """
        provider = None
        try:
            pid = self._caption_provider_id()
            if pid and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(pid)
        except Exception:
            provider = None
        if provider is None:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
        if provider is None:
            return ""
        try:
            if include_hair:
                prompt = (
                    f"这张图是动漫角色「{cos_name}」。先描述她身上的服装和配饰："
                    "款式、颜色、材质、长短袖、裙子还是裤子、鞋袜、头饰，从领口到脚；"
                    "最后补一句她的发色、发型和头发长度（用于复刻）。"
                    "不要描述脸和背景。用 40~150 字中文回答。"
                )
            else:
                prompt = (
                    f"这张图是动漫角色「{cos_name}」。只描述她身上的服装和配饰："
                    "款式、颜色、材质、长短袖、裙子还是裤子、鞋袜、头饰，从领口到脚。"
                    "不要描述发色、发型、脸和背景。用 30~120 字中文回答。"
                )
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, image_urls=[path]),
                timeout=45,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return text[:300]
        except Exception as e:
            logger.warning(f"[selfie] 描述角色服装失败: {e!r}")
            return ""

    def _self_note_provider_id(self) -> str:
        pid = str(self._cfg("self_note_provider_id", "") or "").strip()
        return pid or self._caption_provider_id()

    async def _describe_sent_image(self, path: str, kind: str = "自拍") -> str:
        """用视觉模型看一眼刚发出去的图，写一段「这张图里有什么」的自述。"""
        if not bool(self._cfg("self_note_enabled", True)):
            return ""
        path = str(path or "")
        if not path or not os.path.exists(path):
            return ""
        provider = None
        try:
            pid = self._self_note_provider_id()
            if pid and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(pid)
        except Exception:
            provider = None
        if provider is None:
            logger.warning("[selfie] 没有可用的看图模型，发图自述跳过")
            return ""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=SELFIE_NOTE_PROMPT.format(kind=kind),
                    image_urls=[path],
                ),
                timeout=SELFIE_NOTE_TIMEOUT,
            )
        except Exception as e:
            logger.warning(f"[selfie] 看图自述失败: {e!r}")
            return ""
        text = (getattr(resp, "completion_text", "") or "").strip()
        return self._normalize_visual(text)

    @staticmethod
    def _normalize_visual(text: str) -> str:
        """看图模型有时会自作主张判断「像不像本人」；照片本来就是她本人，统一纠正掉。"""
        s = str(text or "").strip()
        if not s:
            return ""
        fixes = (
            ("不是我本人", "是我本人"),
            ("非我本人", "是我本人"),
            ("不是本人", "是本人"),
            ("非本人", "是本人"),
            ("不是同一个人", "就是本人"),
            ("无本人出镜", "有本人出镜"),
            ("本人未出镜", "本人在照片里"),
            ("本人不在照片中", "本人在照片中"),
            ("没有本人", "有本人"),
            ("不是我", "是我"),
            ("不像我", "就是本人"),
            ("与我不符", "就是本人"),
        )
        for old, new in fixes:
            s = s.replace(old, new)
        return re.sub(r"\s+", " ", s).strip()[:300]

    @staticmethod
    def _note_kind(source) -> str:
        s = str(source or "")
        if s.startswith("closeup_cos"):
            return "特写 cosplay"
        if s.startswith("closeup"):
            return "特写"
        if s.startswith("interact_cos"):
            return "互动 cosplay"
        if s.startswith("interact"):
            return "互动"
        if s == "cos":
            return "cosplay"
        return "自拍"

    def _schedule_self_note(self, entry: dict | None) -> None:
        """后台补一张照片的「照片描述」（同一张只会补一次）。"""
        if not isinstance(entry, dict) or entry.get("visual"):
            return
        if not str(entry.get("path") or ""):
            return
        if not bool(self._cfg("self_note_enabled", True)):
            return
        try:
            self._spawn_task(self._fill_visual(entry))
        except Exception:
            pass

    async def _fill_visual(self, entry: dict) -> None:
        path = str(entry.get("path") or "")
        if not path or entry.get("visual"):
            return
        inflight = getattr(self, "_note_inflight", None)
        if inflight is None:
            inflight = set()
            self._note_inflight = inflight
        if path in inflight:
            return
        inflight.add(path)
        try:
            sem = getattr(self, "_note_sem", None)
            if sem is None:
                sem = asyncio.Semaphore(2)
                self._note_sem = sem
            async with sem:
                kind = self._note_kind(entry.get("source"))
                visual = await self._describe_sent_image(path, kind)
                if not visual or entry.get("visual"):
                    return
                entry["visual"] = visual
                entry["visual_ts"] = self.clock.time()
                self._save_state()
                logger.info(
                    f"[selfie] 发图自述已生成（{kind}, {self._fmt_ts(entry.get('ts'))}）："
                    f"{visual[:60]}"
                )
        except Exception as e:
            logger.warning(f"[selfie] 保存发图自述失败: {e!r}")
        finally:
            inflight.discard(path)

    async def _backfill_visuals(self, max_items: int = 20) -> None:
        """插件启动/重载后，把最近还没自述的照片慢慢补上（同一张只补一次）。"""
        try:
            await asyncio.sleep(15)  # 等启动稳定、别和别的初始化抢资源
            if not bool(self._cfg("self_note_enabled", True)):
                return
            if not bool(self._cfg("selfie_memory_enabled", True)):
                return
            now = self.clock.time()
            missing = []
            for e in (self.state.get("selfies") or []):
                if not isinstance(e, dict) or e.get("visual"):
                    continue
                path = str(e.get("path") or "")
                if not path or not os.path.exists(path):
                    continue
                try:
                    if now - float(e.get("ts") or 0) > SELFIE_NOTE_RECENT_SECONDS:
                        continue
                except Exception:
                    continue
                missing.append(e)
            if not missing:
                return
            todo = missing[-max_items:]
            logger.info(f"[selfie] 开始给 {len(todo)} 张旧照片补描述（后台）")
            for e in todo:
                await self._fill_visual(e)
            logger.info("[selfie] 旧照片描述补完")
        except Exception as e:
            logger.warning(f"[selfie] 启动补自述失败: {e!r}")

    @staticmethod
    def _activity_phrase(activity) -> str:
        a = str(activity or "某处").strip() or "某处"
        return a if a.startswith("在") else "在" + a

    def _format_memory_entry(self, e: dict) -> str:
        kind = self._note_kind(e.get("source"))
        line = (
            f"- {self._fmt_ts(e.get('ts'))}（{kind}）{self._activity_phrase(e.get('activity'))}，"
            f"穿{e.get('outfit') or '平时的衣服'}"
        )
        visual = self._normalize_visual(e.get("visual") or "")
        if visual:
            line += f"。照片里：{visual}"
        else:
            line += f"，背景是{(e.get('desc') or '')[:40]}"
        cap = str(e.get("caption") or "").strip()
        if cap:
            line += f"（你当时配文：「{cap}」）"
        return line

    def _cos_hair_from_character(self) -> bool:
        """cosplay 是否连头发一起换成角色的（配置项 cos_hair_from_character）。"""
        return bool(self._cfg("cos_hair_from_character", True))

    def _cosplay_look(self, cos_name: str, costume_desc: str) -> dict:
        """cos 时换服装和配饰。

        配置项 cos_hair_from_character 打开时，头发颜色、发型和长度也换成 cos 角色的；
        关掉则保持她自己参考图里的头发，只换服装和配饰。
        """
        hair_from_character = self._cos_hair_from_character()
        if hair_from_character:
            no_hat_clause = ("脸保持她自己参考图里的样子；发型、发色和头发长度按 cos 角色来，"
                             "可以戴角色自己的发饰/头饰")
            edit_note = "脸部保持参考图里的同一个人；头发换成 cos 角色的发型和发色；"
        else:
            no_hat_clause = ("脸、发型和发色保持她自己参考图里的样子（只换成角色的服装，"
                             "可以戴角色自己的发饰/头饰）")
            edit_note = "脸部、发型和发色保持参考图里的同一个人；"
        return {
            "hat": "", "hair": "", "necklace": False,
            "outer": "", "inner": "", "bottom": "", "extra": "",
            "full": costume_desc,
            "name": f"cos {cos_name}", "caption": f"cos {cos_name}",
            # 脸不能换：角色图只取服装/头发，禁止把她的脸和角色图融合
            "negative_extra": ["帽子", "外套", "校服", "换脸", "不同的脸",
                               "脸型改变", "五官改变", "参考图2的脸", "美颜"],
            "restyle": True,
            "cos_hair_from_character": hair_from_character,
            "no_hat_clause": no_hat_clause,
            "edit_note": edit_note,
        }

    def _cos_face_lock(self, with_char_image: bool, hair_from_character: bool,
                       realistic: bool = True) -> str:
        """cosplay 的脸部硬性约束：脸必须 1:1 复刻她自己的参考图，不许被角色图带偏。

        双图时有两条「脸」来源（她自己的参考图 + 角色图），模型很容易把两张脸融合，
        所以这里明确说清楚：角色图只取服装/头发，角色图的脸一律不要。
        """
        texture = "脸必须是真人照片的五官和皮肤质感，不许二次元化；" if realistic else ""
        if with_char_image:
            take = "服装、配饰" + ("、发型和发色" if hair_from_character else "")
            return (
                f"脸部硬性约束（最高优先级，任何情况下都不能改）：{texture}"
                "照片里人物的脸必须和参考图1完全一致，是同一张脸的一比一复刻——"
                "眼睛大小和眼距、鼻子、嘴型、脸型轮廓、下颌线和肤色都严格照参考图1，"
                "不许换脸、不许美颜、不许改五官；参考图2只用来取" + take + "，"
                "参考图2的脸和五官一律不要，两张参考图不是同一个人，"
                "照片里只能出现参考图1这张脸。"
            )
        return (
            f"脸部硬性约束（最高优先级，任何情况下都不能改）：{texture}"
            "照片里人物的脸必须和参考图完全一致，是同一张脸的一比一复刻——"
            "眼睛大小和眼距、鼻子、嘴型、脸型轮廓、下颌线和肤色都严格照参考图，"
            "不许换脸、不许美颜、不许改五官。"
        )

    @staticmethod
    def _cos_face_remind(with_char_image: bool) -> str:
        """提示词结尾再强调一次脸部约束（模型对开头和结尾最敏感）。"""
        if with_char_image:
            return ("再次强调：照片里人物的脸必须和参考图1是同一张脸，一比一一致，"
                    "五官不许有任何改动，也不许掺进参考图2的长相。")
        return "再次强调：照片里人物的脸必须和参考图是同一张脸，一比一一致，五官不许有任何改动。"

    def _cosplay_prompt(self, scene: dict, model: str | None, style: str | None,
                        look: dict, cos_name: str, costume_desc: str,
                        with_char_image: bool = True) -> str:
        use_ref = self._use_reference(model, style)
        style = self._style(style)
        style_block = STYLE_BLOCK[style]
        expression = self.rng.choice(EXPRESSIONS)
        gesture = self.rng.choice(GESTURES)
        hair_from_character = bool(look.get("cos_hair_from_character"))
        face_lock = ""
        face_remind = ""
        if use_ref:
            face_lock = self._cos_face_lock(with_char_image, hair_from_character,
                                            style == STYLE_REALISTIC)
            face_remind = self._cos_face_remind(with_char_image)
        if hair_from_character:
            no_hat = "脸保持参考图1里的同一个人，不要改"
            cos_line = (
                f"这是一次 cosplay：她 cosplay 成《{cos_name}》，"
                f"只穿这个角色的服装和配饰：{costume_desc}。"
                "发型、发色和头发长度也按这个角色来，不要保留她平时自己的头发。"
            )
        else:
            no_hat = "脸、发型和发色保持参考图1里的同一个人，不要改"
            cos_line = (
                f"这是一次 cosplay：她 cosplay 成《{cos_name}》，"
                f"只穿这个角色的服装和配饰：{costume_desc}。"
            )
        if use_ref:
            if style == STYLE_REALISTIC:
                if hair_from_character:
                    role = (
                        "请以参考图1里同一位成年女性的脸为准（真人照片，不要二次元化），"
                        "服装、配饰、发型和发色都按参考图2"
                    )
                    ref_self = "她本人（同一个成年女性的脸和身体）"
                    same_person = ("脸保持参考图1里同一个人（真人五官，不要二次元化），"
                                   "发型和发色按 cos 角色")
                else:
                    role = (
                        "请以参考图1里同一位成年女性的脸和发型为准（真人照片，不要二次元化），"
                        "参考图2只是要 cos 的角色服装样式"
                    )
                    ref_self = "角色本人（同一个成年女性的脸、发型和发色）"
                    same_person = "脸和发型保持参考图1里同一个人（真人五官，不要二次元化）"
            else:
                if hair_from_character:
                    role = (
                        "请以参考图1里的同一个二次元角色（她本人的脸）为准，"
                        "服装、配饰、发型和发色都按参考图2"
                    )
                    ref_self = "她本人（同一个二次元角色）"
                    same_person = "脸保持参考图1里同一个角色，发型和发色按 cos 角色"
                else:
                    role = "请以参考图中的同一个二次元角色"
                    ref_self = "角色本人（同一个二次元角色）"
                    same_person = "脸和发型保持参考图1里同一个角色"
            if hair_from_character:
                if with_char_image:
                    base_line = ("角色基础形象：同一张脸，发型、发色和头发长度按参考图2"
                                 "的 cos 角色来，{no_hat}；")
                else:
                    base_line = ("角色基础形象：同一张脸，发型、发色和头发长度按下面这段"
                                 "角色描述来做，{no_hat}；")
            else:
                base_line = "角色基础形象必须和参考图1完全一致：同一张脸、同样的发型和发色，{no_hat}；"
            if with_char_image:
                ref_desc = f"参考图1是{ref_self}，参考图2是要 cos 的角色形象图。"
            else:
                ref_desc = f"参考图是{ref_self}。"
                no_hat = no_hat.replace("参考图1里", "参考图里")
                same_person = same_person.replace("参考图1里", "参考图里")
                if not hair_from_character:
                    base_line = base_line.replace("参考图1", "参考图里")
                if hair_from_character:
                    # 单图站点没有角色图可参考：服装、发型都按下面这段角色描述来做
                    role = "cos 角色的服装、配饰、发型和发色都按下面这段角色描述来做"
                else:
                    role = "cos 角色的服装和配饰按下面这段角色描述来做"
            head = (
                f"{ref_desc}"
                f"{face_lock}"
                f"{role}，拍一张手机前置摄像头自拍的第一人称视角照片，9:16竖屏构图，"
                "近距离自拍特写（只拍到腰部以上，脸和上半身占满整张照片）："
                f"{style_block}"
                f"{base_line.format(no_hat=no_hat)}"
                f"{cos_line}"
                "镜头就在正前方，她朝镜头伸出一只手臂，前臂从镜头右下方斜着伸向镜头、"
                "手掌在镜头外，照片里只有这条手臂；"
                f"另一只手{gesture}；脸在照片上方占三分之一，{expression}，眼睛直视镜头。"
                f"{same_person}，衣服严格按上面这段描述来拍。"
            )
        else:
            head = (
                "手机前置摄像头自拍的第一人称视角照片，9:16竖屏构图，"
                "近距离自拍特写（只拍到腰部以上，脸和上半身占满整张照片）："
                f"{style_block}"
                "镜头就在正前方，她朝镜头伸出一只手臂，前臂从镜头右下方斜着伸向镜头、"
                "手掌在镜头外，照片里只有这条手臂；"
                f"另一只手{gesture}；脸在照片上方占三分之一，{expression}，眼睛直视镜头。"
                f"人物：参考图里的同一位成年女性，{no_hat}；穿{costume_desc}。"
                f"{cos_line}"
            )
        return (
            head
            + scene_context_text(scene)
            + f"场景：{scene.get('desc', '')}，背景是真实世界的环境，"
            "有清晰的环境细节、真实材质和自然光影，背景轻微虚化。"
            + STYLE_QUALITY[style]
            + self._scene_hard_note(scene)
            + face_remind
        )

    def _cos_self_ref(self, scene: dict | None = None,
                      style: str | None = None) -> str:
        """cos 出图时要贴的她自己的那张参考图（角色图作为第二张）。

        全部来自工作台「参考图」页：写实风格优先头部参考图（脸和头发），
        二次元风格用主站参考图1；没配置时返回空串，出图阶段会提示先去参考图页上传。
        """
        try:
            plan = self._ref_plan(scene, style, {"restyle": True})
        except Exception as e:  # noqa: BLE001 - 参考图库异常不能卡住 cos
            logger.warning(f"[selfie] 读取 cos 参考图失败: {e!r}")
            return ""
        for key in ("ref", "single_ref"):
            path = str(plan.get(key) or "")
            if path:
                return path
        return ""

    async def _generate_cosplay(self, scene: dict, model: str, style: str,
                                look: dict, entry: dict) -> bytes:
        """优先「她自己 + 角色图」双图生成；单图站点 / 双图失败退回先描述服装再单图生成。"""
        cos_name = str(entry.get("name") or "")
        char_path = str(entry.get("path") or "")
        # 角色图只是第二张参考图；第一张必须是她自己的参考图，否则会被当成「没有参考图」
        self_ref = self._cos_self_ref(scene, style)
        two = bool(char_path and os.path.exists(char_path)
                   and self_ref and self._site_max_refs() >= 2)
        refs = ({"main_refs": (self_ref, char_path), "backup_refs": (self_ref,)}
                if two else {"main_refs": (self_ref,), "backup_refs": (self_ref,)})
        prompt = self._cosplay_prompt(
            scene, model, style, look, cos_name, look.get("full") or "",
            with_char_image=two)
        if two:
            try:
                return await self._generate(
                    prompt, model, style, look.get("negative_extra"), **refs)
            except Exception as e:
                logger.warning(f"[selfie] 双图 cosplay 生成失败，改用描述法: {e!r}")
        desc = await self._describe_character_costume(
            char_path, cos_name, include_hair=self._cos_hair_from_character()
        ) or look.get("full") or ""
        look2 = dict(look)
        look2["full"] = desc
        prompt2 = self._cosplay_prompt(
            scene, model, style, look2, cos_name, desc, with_char_image=False
        )
        return await self._generate(prompt2, model, style, look2.get("negative_extra"),
                                    main_refs=(self_ref,), backup_refs=(self_ref,))

    async def _start_cos_request(self, event, sender: str, cos_name: str,
                                 mode: str = "selfie", cue: dict | None = None) -> None:
        try:
            await event.send(MessageChain([Plain(f"我去搜一下「{cos_name}」长什么样～")]))
            candidates = await self._search_character_images(cos_name)
            if not candidates:
                await event.send(MessageChain([
                    Plain(f"没搜到「{cos_name}」的图……给我个日文名/英文名，或者说得更具体一点？")
                ]))
                return
            self._prune_cos_files()
            self.state["cos_pending"] = {
                "umo": str(getattr(event, "unified_msg_origin", "") or ""),
                "sender": str(sender),
                "query": cos_name,
                "candidates": candidates,
                "idx": 0,
                "ts": self.clock.time(),
                # mode=closeup 表示这次 cos 是「特写 cosplay」，确认后走特写流程
                "mode": mode,
                "cue": cue or {},
            }
            self._save_state()
            first = candidates[0]
            await event.send(MessageChain([
                Image.fromFileSystem(first["path"]),
                Plain(f"我找到的是「{first['name']}」，是这个角色吗？（回「是」我就开始 cos；不是的话我换一张）"),
            ]))
            logger.info(f"[selfie] cos 候选已发送: {first['name']}（{first.get('source')}）")
        except Exception as e:
            logger.error(f"[selfie] 搜索 cos 角色失败: {e!r}")
            try:
                await event.send(MessageChain([Plain("搜图的时候出错了，等会儿再试？")]))
            except Exception:
                pass
        finally:
            self._generating = False

    async def _next_cos_candidate(self, event) -> None:
        pending = self.state.get("cos_pending")
        if not isinstance(pending, dict):
            return
        text = self._clean_text(event)
        new_name = self._detect_cos(text)
        if not new_name:
            # 「不是，是雷姆」：从否定句里把新角色名抠出来
            m = re.search(
                r"(?:不是|不对|错了)[，,。\s]*(?:是|换成|改成|其实)?\s*"
                r"([\u4e00-\u9fa5A-Za-z0-9· ]{2,20})",
                text,
            )
            if m:
                cand_name = re.sub(r"[吧呢啊嘛呀哦的啦~。!?，,\s]+$", "", m.group(1).strip()).strip()
                if 2 <= len(cand_name) <= 20:
                    new_name = cand_name
        old_query = str(pending.get("query") or "")
        if new_name and new_name != old_query and old_query not in new_name:
            await self._start_cos_request(
                event,
                str(event.get_sender_id()),
                new_name,
                str(pending.get("mode") or "selfie"),
                pending.get("cue") if isinstance(pending.get("cue"), dict) else None,
            )
            return
        candidates = pending.get("candidates") or []
        idx = int(pending.get("idx") or 0) + 1
        if idx >= len(candidates):
            self.state["cos_pending"] = {}
            self._save_state()
            await event.send(MessageChain([
                Plain("搜到的几张都不是吗……那你告诉我更准确的名字（日文/英文名也行）")
            ]))
            return
        pending["idx"] = idx
        pending["ts"] = self.clock.time()
        self.state["cos_pending"] = pending
        self._save_state()
        item = candidates[idx]
        await event.send(MessageChain([
            Image.fromFileSystem(item["path"]),
            Plain(f"那这张呢？「{item['name']}」"),
        ]))

    async def _handle_cos_confirmation(self, event, text: str) -> bool:
        """处理「是不是这个角色」的回复。返回 True 表示已消费这条消息。"""
        pending = self.state.get("cos_pending")
        if not isinstance(pending, dict) or not pending.get("candidates"):
            return False
        if pending.get("umo") != str(getattr(event, "unified_msg_origin", "") or ""):
            return False
        if str(pending.get("sender")) != str(event.get_sender_id()):
            return False
        if self.clock.time() - float(pending.get("ts") or 0) > COS_PENDING_TTL:
            self.state["cos_pending"] = {}
            self._save_state()
            return False
        # 太长的句子当普通聊天，不当成确认/否认
        new_name = self._detect_cos(text)
        if len(text.strip()) > 24 and not new_name:
            return False
        # 「不是，是雷姆」这种：直接换搜新角色
        old_query = str(pending.get("query") or "")
        if new_name and new_name != old_query and old_query not in new_name:
            await self._start_cos_request(
                event,
                str(event.get_sender_id()),
                new_name,
                str(pending.get("mode") or "selfie"),
                pending.get("cue") if isinstance(pending.get("cue"), dict) else None,
            )
            return True
        core = re.sub(r"[吧呢啊嘛呀哦的啦~。!?，,.\s]+$", "", text.strip())
        if COS_NO_PAT.search(core) or core in ("不", "否"):
            await self._next_cos_candidate(event)
            return True
        if not COS_YES_PAT.match(core):
            return False
        if not bool(self._cfg("cos_enabled", True)):
            self.state["cos_pending"] = {}
            self._save_state()
            return False
        candidates = pending.get("candidates") or []
        idx = int(pending.get("idx") or 0)
        idx = max(0, min(idx, len(candidates) - 1))
        cand = candidates[idx]
        mode = str(pending.get("mode") or "selfie")
        cue = pending.get("cue") if isinstance(pending.get("cue"), dict) else None
        self.state["cos_pending"] = {}
        self._save_state()
        sender = str(event.get_sender_id())
        if mode == "closeup":
            if self._natural_chat_enabled():
                ack = await self._natural_ack(
                    event,
                    action_line=f"cos「{cand['name']}」，换好衣服后拍一张近距离特写",
                    detail="对方刚确认要这个角色，你已经答应下来了。",
                    fallback_pool=[f"我要 cos {cand['name']} 啦，等我换好给你拍张特写"],
                )
            else:
                ack = f"我要 cos {cand['name']} 啦，等我换好给你拍张特写～"
            await self._send_reply(event, ack)
            self._generating = True
            self._spawn_task(self._do_closeup(
                event, sender in self._admins(), sender, self._user_style(sender),
                None, None, cue, cand,
            ))
            return True
        if self._natural_chat_enabled():
            ack = await self._natural_ack(
                event,
                action_line=f"cos「{cand['name']}」，换好衣服后拍一张自拍",
                detail="对方刚确认要这个角色，你已经答应下来了。",
                fallback_pool=[f"我要 cos {cand['name']} 啦，等我换好"],
            )
        else:
            ack = f"我要 cos {cand['name']} 啦，等我换好🤳"
        await self._send_reply(event, ack)
        self._generating = True
        self._spawn_task(self._do_cosplay(event, sender, cand))
        return True

    async def _do_cosplay(self, event, sender: str, cand: dict) -> None:
        try:
            is_admin = sender in self._admins()
            if not is_admin and self._quota_left(sender) <= 0:
                await event.send(MessageChain([
                    Plain(f"今天已经拍够{self._daily_limit()}张啦，明天再看吧～")
                ]))
                return
            scene = await self._get_scene()
            cos_name = str(cand.get("name") or "角色")
            model = str(self._cfg("model", DEFAULT_SELFIE_MODEL))
            style = self._style(self._user_style(sender))
            costume_desc = (
                await self._describe_character_costume(str(cand.get("path") or ""), cos_name)
                or f"《{cos_name}》这个角色身上的整套服装和配饰（按参考图2）"
            )
            look = self._cosplay_look(cos_name, costume_desc)
            img = await self._generate_cosplay(scene, model, style, look, cand)
            caption = f"给你～我 cos 的 {cos_name}（{self._scene_caption_tail(scene)}）"
            caption = await self._deliver_photo(
                event, img, scene=scene, look=look, kind="cos 照片",
                cos_name=cos_name, fallback_caption=caption,
            )
            entry = self._save_selfie(
                img, scene, look, str(getattr(event, "unified_msg_origin", "") or ""),
                sender, caption, source="cos",
            )
            self._schedule_self_note(entry)
            if not is_admin:
                self._bump_quota(sender)
            logger.info(f"[selfie] cosplay 完成: {cos_name}（source={cand.get('source')}）")
        except Exception as e:
            logger.error(f"[selfie] cosplay 生成失败: {e!r}")
            try:
                tip = self._generation_failure_text(e, generic="这次没换好……等我缓缓再试")
                await event.send(MessageChain([Plain(tip)]))
            except Exception:
                pass
        finally:
            self._generating = False

    # ------------------------------------------------------------------
    # 特写：生成 + 发送（普通特写 / 特写 cosplay / 带换装的特写）
    # ------------------------------------------------------------------
    async def _generate_closeup(self, scene: dict, model: str, style: str,
                                look: dict, cue: dict | None,
                                cos_name: str = "", char_path: str = "",
                                kind: str = "", refs: tuple | None = None) -> bytes:
        """生成特写：普通特写单图；特写 cos 先试双图，失败退回「先描述服装」再单图。

        refs 由生图测试显式传（参考图衣服档 / 素颜勾选）；None 时按当天规则自动挑。
        """
        if (cue or {}).get("sketch_id") is not None:
            if cos_name:
                raise ValueError("线稿参考特写暂不与角色参考图混用，请单独选择线稿动作")
            return await self._generate_sketch_closeup(scene, model, look, cue, refs=refs)
        cue_text = self._clean_closeup_cue((cue or {}).get("text"))
        # cos 特写最多两张参考图：她自己 + 角色图，不再额外掺素颜参考图，免得超过站点张数上限
        cos = bool(cos_name)
        head_only = False
        gen_refs = {}
        cos_refs = {}
        cos_two = False
        self_ref = ""
        if cos:
            # 角色图只是第二张参考图；第一张必须是她自己的参考图，否则会被当成「没有参考图」
            self_ref = self._cos_self_ref(scene, style)
            cos_two = bool(char_path and os.path.exists(char_path)
                           and self_ref and self._site_max_refs() >= 2)
            cos_refs = ({"main_refs": (self_ref, char_path), "backup_refs": (self_ref,)}
                        if cos_two else
                        {"main_refs": (self_ref,), "backup_refs": (self_ref,)})
            ref, face_ref, single_ref = None, None, None
            prompt_refs = None
        else:
            explicit_refs = refs is not None
            if explicit_refs:
                ref, face_ref, single_ref = refs
                head_only = self._restyle_head_only(scene, style, look)
                gen_refs = {"ref": ref, "face_ref": face_ref, "single_ref": single_ref,
                            "head_only": head_only}
            else:
                gen_refs = self._generation_refs(scene, style, look)
                ref, face_ref, single_ref = (gen_refs.get("ref"), gen_refs.get("face_ref"),
                                             gen_refs.get("single_ref"))
                head_only = bool(gen_refs.get("head_only"))
            # 没有覆盖时把 refs 传 None，让提示词按原来的「自动挑参考图」走；
            # 显式传了覆盖（哪怕只有素颜脸）才锁成 use_ref。
            prompt_refs = ((ref, face_ref, single_ref)
                           if explicit_refs or ref or face_ref or single_ref else None)
        prompt_look = look
        if cos and not cos_two:
            # 单图站点没有角色图可参考：先把角色的服装（连头发）描述出来再单图生成
            desc = (
                await self._describe_character_costume(
                    char_path, cos_name, include_hair=self._cos_hair_from_character())
                or look.get("full") or ""
            )
            prompt_look = dict(look)
            prompt_look["full"] = desc
        prompt = self._closeup_prompt(
            scene, model, style, prompt_look, cue,
            cos_name=cos_name, with_char_image=cos_two, kind=kind,
            refs=prompt_refs,
        )
        # 最后一道：让 LLM 把动作落进当前场景（服装/动作/机位不动，防悬浮和背景畸形）
        prompt = await self._scene_adapt_prompt(prompt, scene, prompt_look, cue_text)
        neg = list(look.get("negative_extra") or [])
        if CLOSEUP_TOE_PAT.search(cue_text):
            neg += CLOSEUP_NEG_TOE
        if CLOSEUP_HAND_PAT.search(cue_text):
            neg += CLOSEUP_NEG_HAND
        if cos_two:
            try:
                return await self._generate(prompt, model, style, neg, **cos_refs)
            except Exception as e:
                logger.warning(f"[selfie] 特写 cos 双图生成失败，改用描述法: {e!r}")
                desc = (
                    await self._describe_character_costume(
                        char_path, cos_name,
                        include_hair=self._cos_hair_from_character())
                    or look.get("full") or ""
                )
                look2 = dict(look)
                look2["full"] = desc
                prompt2 = self._closeup_prompt(
                    scene, model, style, look2, cue,
                    cos_name=cos_name, with_char_image=False, kind=kind,
                )
                prompt2 = await self._scene_adapt_prompt(prompt2, scene, look2, cue_text)
                return await self._generate(
                    prompt2, model, style, neg,
                    main_refs=(self_ref,), backup_refs=(self_ref,))
        if cos:
            return await self._generate(prompt, model, style, neg, **cos_refs)
        return await self._generate(prompt, model, style, neg, **gen_refs)

    async def _generate_sketch_closeup(self, scene: dict, model: str, look: dict,
                                       cue: dict, refs: tuple | None = None) -> bytes:
        """线稿始终是第二张图，只取动作与机位，不使用线稿的人物外观"""
        library = self._pose_sketch_library()
        item = library.get(cue.get("sketch_id"))
        pose_image = library.image_path(cue.get("sketch_id")) if item else ""
        if not pose_image:
            raise ValueError("线稿参考特写不存在，请刷新线稿列表后重试")
        style = STYLE_REALISTIC
        if refs is None:
            gen_refs = self._generation_refs(scene, style, look)
        else:
            ref, face, single = refs
            gen_refs = {"ref": ref, "face_ref": face, "single_ref": single,
                        "head_only": self._restyle_head_only(scene, style, look)}
        # 两图站点给身份留一个槽位，另一个槽位必须留给线稿
        main = tuple(gen_refs.get("main_refs") or ())
        backup = tuple(gen_refs.get("backup_refs") or ())
        # 身份必须优先使用清晰面部图，不能让全身图的小脸挤掉专门的面部参考
        face = gen_refs.get("face_ref") or ""
        if refs is None or any(refs):
            head = self._head_only_refs(scene, style)
            face = (self._ref_sleep_face_image() if self._bare_face_ref_active(scene, style)
                    else face or (head[0] if head else ""))
        primary = face or next((x for x in main if x), "") or gen_refs.get("ref") \
            or gen_refs.get("single_ref") or ""
        secondary_site = primary
        # 三图站点还能带上衣服图，传输层按容量截取，但面部和线稿永远保留
        outfit_main = next((x for x in main if x and x != primary), "") or gen_refs.get("ref") or ""
        outfit_backup = next((x for x in backup if x and x != primary), "") or gen_refs.get("single_ref") or outfit_main
        main_images = tuple(dict.fromkeys(x for x in (primary, outfit_main) if x))
        backup_images = tuple(dict.fromkeys(x for x in (primary, outfit_backup) if x))
        if not primary:
            raise ValueError("还没有可用的参考图：请打开插件页「自拍工作台 → 参考图」，上传并填好主站 / 副站参考图，再回来拍照")
        prompt_refs = (primary, None, secondary_site)
        face_lock = (
            "脸部硬性约束（最高优先级）：参考图1是唯一的本人面部身份参考，最终人物的脸必须与"
            "参考图1一比一一致，是同一张脸的一比一复刻，眼睛形状与大小、眼距、鼻梁与鼻翼、"
            "嘴型与唇形、脸型轮廓、下颌线、五官比例和肤色都必须严格符合参考图1"
            "只允许随着机位产生合理透视、表情和光影变化，禁止换脸、改五官、改变脸型、"
            "美颜或生成另一个人；参考图2只指导动作和机位，其头部只取朝向和角度，"
            "禁止采用或融合线稿人体模型的脸型、五官、长相与人物身份"
        )
        # 不叠加随机文字特写、吃饭/睡觉动作，姿势和裁切全部以线稿为准
        sketch_cue = {"text": "严格按照参考图2白描线稿中人体模型的姿势、肢体关系、镜头角度和构图裁切拍摄"}
        prompt = self._closeup_prompt(scene, model, style, look, sketch_cue, refs=prompt_refs)
        prompt = await self._scene_adapt_prompt(prompt, scene, look, sketch_cue["text"])
        prompt = face_lock + "\n" + prompt
        # 在场景适配之后追加硬约束，防止适配模型抹掉动作图分工
        if primary == self._ref_sleep_face_image():
            prompt += "\n身份参考图是卸妆后的素颜，不要添加眼影、眼线、腮红或口红，保持自然皮肤质感"
        prompt += (
            "\n以下线稿动作要求优先于上文通用特写构图与姿势要求，构图和姿势不冲突地以线稿为准"
            "\n参考图分工：参考图1只确定本人面部身份和发型；服装按上面的服装要求，"
            "若收到参考图3则它仅提供服装信息，不能改变参考图1的脸；参考图2是动作白描线稿，"
            "仅作为动作示意图，不是最终画风参考；"
            "严格按照参考图2中人体模型的动作、身体朝向、四肢位置、支撑接触关系、镜头俯仰角度、"
            "机位、透视及特写裁切摆姿势，不要改成其它动作或默认自拍姿势"
            "线稿中的人体模型只是动作示意，最终画面中的人物必须是参考图1里的真人，"
            "皮肤真实自然，有自然肤色、细微皮肤纹理和真实摄影光影，不要线条描边、插画、"
            "素描、白色人体模型、塑料皮肤或人偶质感，不要把线稿本身画进最终照片"
        )
        prompt += "\n再次强调：脸必须与参考图1一比一一致，不能换脸、改五官或融合线稿中的长相；动作与机位才按照参考图2"
        negative = list(look.get("negative_extra") or []) + [
            "线稿", "白描", "素描", "人体模型", "塑料皮肤", "插画", "换脸", "不同的脸",
            "五官改变", "脸型改变", "融合线稿人物长相", "参考图2的脸", "美颜"]
        return await self._generate(prompt, model, style, negative,
                                    main_refs=main_images, backup_refs=backup_images,
                                    pose_image=pose_image)

    async def _generate_selfie_shot(self, scene: dict, model: str, style: str,
                                    look: dict, action: dict,
                                    refs: tuple | None = None) -> bytes:
        """生图测试「自拍」来源：用选中的表情 / 手势拍一张前置摄像头自拍。"""
        explicit_refs = refs is not None
        if explicit_refs:
            ref, face_ref, single_ref = refs
            gen_refs = {"ref": ref, "face_ref": face_ref, "single_ref": single_ref,
                        "head_only": self._restyle_head_only(scene, style, look)}
        else:
            gen_refs = self._generation_refs(scene, style, look)
            ref, face_ref, single_ref = (gen_refs.get("ref"), gen_refs.get("face_ref"),
                                         gen_refs.get("single_ref"))
        prompt_refs = ((ref, face_ref, single_ref)
                       if explicit_refs or ref or face_ref or single_ref else None)
        prompt = self._selfie_prompt(scene, model, style, look, selfie_action=action,
                                     refs=prompt_refs)
        return await self._generate(prompt, model, style, look.get("negative_extra"),
                                    **gen_refs)

    async def _do_closeup(self, event: AstrMessageEvent, is_admin: bool,
                          sender: str = "", style_override: str | None = None,
                          removal: dict | None = None,
                          costume: dict | None = None,
                          cue: dict | None = None,
                          cand: dict | None = None,
                          force_costume: bool = False,
                          kind: str = "") -> None:
        """拍一张特写。cand 有值就是「特写 cosplay」，否则按当时的衣服拍。

        kind 有值（摸摸 / 捏捏 / 戳戳 / 揉揉 / 拍拍…）= 互动特写：构图话术换成
        「镜头外伸一只手进来」的版本，记忆里也标成互动，别的流程（场景/衣服/额度/配额）完全共用。
        """
        cos_name = ""
        try:
            cue = cue if isinstance(cue, dict) else None
            if (not is_admin) and self._quota_left(sender) <= 0:
                await event.send(MessageChain([
                    Plain(f"今天已经拍够{self._daily_limit()}张啦，明天再给你看～")
                ]))
                return
            # 事件里的特写/互动只用这次的 cue，不叠加事件动作（用户点单特写时
            # 只要特写提示词）；场景仍沿用事件当前地点。
            scene = self._event_photo_scene(await self._get_scene())[0]
            model = str(self._cfg("model", DEFAULT_SELFIE_MODEL))
            style = STYLE_REALISTIC if (cue or {}).get("sketch_id") is not None else self._style(style_override)
            await self._ensure_ref_day(scene)
            cos_name = str((cand or {}).get("name") or "")
            char_path = str((cand or {}).get("path") or "")
            if cos_name:
                costume_desc = (
                    await self._describe_character_costume(char_path, cos_name)
                    or f"《{cos_name}》这个角色身上的整套服装和配饰（按参考图2）"
                )
                look = self._cosplay_look(cos_name, costume_desc)
            else:
                look = self._resolve_look(scene, removal, None, costume, force_costume,
                                          style=style)
            kind_name = str(kind or "").strip()
            # 互动提示词本来就要「镜头外有只手入镜」，生成期间临时剔掉会掐掉那只手的负面词
            self._neg_filter = INTERACT_NEG_DROP if kind_name else None
            try:
                img = await self._generate_closeup(
                    scene, model, style, look, cue,
                    cos_name=cos_name, char_path=char_path, kind=kind_name,
                )
            finally:
                self._neg_filter = None
            base_tag = kind_name or "特写"
            tag = f"{base_tag}·cos {cos_name}" if cos_name else base_tag
            caption = f"给你～（{tag}，{self._scene_caption_tail(scene)}）"
            look_phrase = self._look_caption(look) if not cos_name else ""
            if look_phrase:
                caption = f"给你～（{tag}，{self._scene_caption_tail(scene)}，{look_phrase}）"
            caption = await self._deliver_photo(
                event, img, scene=scene, look=look,
                kind=(f"对方{kind_name}你时的近距离特写" if kind_name else "近距离特写"),
                cos_name=cos_name, fallback_caption=caption,
            )
            if kind_name:
                source = "interact_cos" if cos_name else "interact"
            else:
                source = "closeup_cos" if cos_name else "closeup"
            entry = self._save_selfie(
                img, scene, look, str(getattr(event, "unified_msg_origin", "") or ""),
                sender, caption, source=source,
            )
            self._schedule_self_note(entry)
            if not is_admin:
                self._bump_quota(sender)
            logger.info(
                f"[selfie] 已生成{kind_name or '特写'}（admin={is_admin}, sender={sender}, "
                f"scene={scene.get('type')}, outfit={look.get('name')}, "
                f"cos={cos_name or '-'}, cue=#{(cue or {}).get('id') or (cue or {}).get('sketch_id') or 0}"
                f"({(cue or {}).get('tier') or 'normal'}), "
                f"style={style}, quota_used={self._quota_used(sender) if sender else '-'}）"
            )
            return True
        except Exception as e:
            logger.error(f"[selfie] 特写生成失败: {e!r}")
            try:
                tip = self._generation_failure_text(e, generic=CLOSEUP_FAIL_TEXT)
                await event.send(MessageChain([Plain(tip)]))
            except Exception:
                pass
            return False
        finally:
            self._generating = False

    # ------------------------------------------------------------------
    # 自拍记忆 / 照片里认出自己
    # ------------------------------------------------------------------
    @staticmethod
    def _img_hashes(path: str) -> dict | None:
        """算图片的 dHash / aHash（64 位各一个），用来认「这张是不是我发过的照片」。"""
        try:
            from PIL import Image as PILImage
        except Exception:
            return None
        try:
            with PILImage.open(path) as im:
                im = im.convert("L")
                dz = im.resize((9, 8), PILImage.LANCZOS)
                px = list(dz.getdata())
                dhash = 0
                for row in range(8):
                    for col in range(8):
                        if px[row * 9 + col] > px[row * 9 + col + 1]:
                            dhash |= 1 << (row * 8 + col)
                az = im.resize((8, 8), PILImage.LANCZOS)
                apx = list(az.getdata())
                avg = sum(apx) / 64.0
                ahash = 0
                for i, val in enumerate(apx):
                    if val > avg:
                        ahash |= 1 << i
            return {"dhash": dhash, "ahash": ahash}
        except Exception as e:
            logger.warning(f"[selfie] 计算图片指纹失败（{path}）: {e!r}")
            return None

    @staticmethod
    def _ham(a, b) -> int:
        try:
            return bin(int(a) ^ int(b)).count("1")
        except Exception:
            return 64

    def _ref_hashes(self) -> list[dict]:
        """用参考图页里的图给她自己建指纹，方便认出「对方发回来的是她自己那张」。"""
        paths = []
        try:
            paths.extend(p for p in self._ref_slots().values() if p)
            suyan = self._ref_suyan_path()
            if suyan:
                paths.append(suyan)
        except Exception:  # noqa: BLE001 - 指纹失败不影响出图
            paths = []
        key = tuple(dict.fromkeys(paths))
        cached = getattr(self, "_ref_hash_cache", None)
        if isinstance(cached, tuple) and cached and cached[0] == key:
            return cached[1]
        refs = []
        for path in key:
            hashes = self._img_hashes(path)
            if hashes:
                refs.append(hashes)
        self._ref_hash_cache = (key, refs)
        return refs

    def _selfie_dir(self) -> str:
        d = os.path.join(self.data_dir, "selfies")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def _save_selfie(self, img: bytes, scene: dict, look: dict,
                     umo: str, sender: str, caption: str,
                     source: str = "chat", visual: str = "") -> dict | None:
        """把发出去的自拍存盘并记到 state["selfies"]，模型下次就能想起来。"""
        try:
            path = os.path.join(
                self._selfie_dir(),
                "selfie_" + self.clock.strftime("%Y%m%d_%H%M%S") + "_"
                + str(self.rng.randint(100, 999)) + ".png",
            )
            with open(path, "wb") as f:
                f.write(img)
            hashes = self._img_hashes(path) or {}
            entry = {
                "ts": self.clock.time(),
                "umo": str(umo or ""),
                "sender": str(sender or ""),
                "source": source,
                "scene_type": scene.get("type"),
                "desc": scene.get("desc"),
                "activity": scene.get("activity"),
                "outfit": look.get("name"),
                "caption": caption,
                "path": path,
                "dhash": hashes.get("dhash"),
                "ahash": hashes.get("ahash"),
                "visual": str(visual or ""),
            }
            log = self.state.get("selfies")
            if not isinstance(log, list):
                log = []
            log.append(entry)
            self.state["selfies"] = log[-SELFIE_LOG_LIMIT:]
            self._save_state()
            self._prune_selfie_files()
            try:
                # Workbench album keeps a bounded, browsable copy of every sent photo.
                self._album().save(img, source="selfie", meta={
                    "city": scene.get("city"), "district": scene.get("district"),
                    "place": scene.get("activity"), "costume_name": look.get("name")})
            except Exception as album_err:
                logger.debug(f"[selfie] 自拍入相册失败: {album_err!r}")
            return entry
        except Exception as e:
            logger.warning(f"[selfie] 保存自拍记录失败: {e!r}")
            return None

    def _prune_selfie_files(self) -> None:
        """清掉不在记忆列表里、且超过 3 天的旧自拍文件。"""
        try:
            keep = {
                str(e.get("path"))
                for e in (self.state.get("selfies") or [])
                if isinstance(e, dict) and e.get("path")
            }
            d = os.path.join(self.data_dir, "selfies")
            if not os.path.isdir(d):
                return
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if p in keep:
                    continue
                try:
                    if self.clock.time() - os.path.getmtime(p) > 3 * 86400:
                        os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass

    def _match_selfie(self, hashes: dict) -> dict | None:
        """传来的图片指纹 -> 命中哪张自己发过的自拍。"""
        if not hashes or hashes.get("dhash") is None:
            return None
        best, best_score = None, 1e9
        for entry in self.state.get("selfies") or []:
            if not isinstance(entry, dict) or entry.get("dhash") is None:
                continue
            d = self._ham(entry["dhash"], hashes["dhash"])
            a = self._ham(entry.get("ahash") or 0, hashes.get("ahash") or 0)
            if d <= SELFIE_HASH_MATCH:
                score = d + a * 0.5
                if score < best_score:
                    best, best_score = entry, score
        return best

    def _is_ref_self(self, hashes: dict) -> bool:
        if not hashes:
            return False
        for ref in self._ref_hashes():
            d = self._ham(ref.get("dhash"), hashes.get("dhash"))
            a = self._ham(ref.get("ahash"), hashes.get("ahash"))
            if d <= SELFIE_HASH_MATCH and a <= SELFIE_HASH_MATCH:
                return True
        return False

    async def _event_image_paths(self, event) -> list[str]:
        """当前这条消息里带的图片（包括引用消息里的图），转成本地路径。"""
        comps = []
        try:
            comps = list(event.get_messages() or [])
        except Exception:
            comps = []

        images = []

        def collect(comp):
            if isinstance(comp, Image):
                images.append(comp)
            chain = getattr(comp, "chain", None)
            if isinstance(chain, list):
                for child in chain:
                    collect(child)

        for comp in comps:
            try:
                collect(comp)
            except Exception:
                pass
        paths = []
        for comp in images[:3]:
            try:
                path = await comp.convert_to_file_path()
                if path:
                    paths.append(path)
            except Exception as e:
                logger.info(f"[selfie] 读取消息图片失败: {e!r}")
        return paths

    @staticmethod
    def _fmt_ts(ts) -> str:
        try:
            lt = time.localtime(float(ts))
        except Exception:
            return "刚才"
        if time.strftime("%Y-%m-%d", lt) == time.strftime("%Y-%m-%d"):
            return time.strftime("%H:%M", lt)
        return time.strftime("%m-%d %H:%M", lt)

    @staticmethod
    def _memory_injection_text(value, limit: int) -> str:
        """Bound one display field, not stored data or the completed XML block."""
        text = " ".join(str(value or "").split())
        # Keep user-authored fields from introducing memory wrapper tags/entities.
        text = text.replace("&", "＆").replace("<", "＜").replace(">", "＞")
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _format_injected_memory_entry(self, e: dict) -> str:
        """Request-only compact view; leave shared/archive formatting unchanged."""
        clip = self._memory_injection_text
        line = (
            f"- {clip(self._fmt_ts(e.get('ts')), 20)}"
            f"（{clip(self._note_kind(e.get('source')), 16)}）"
            f"{clip(self._activity_phrase(e.get('activity')), 40)}，"
            f"穿{clip(e.get('outfit') or '平时的衣服', 40)}"
        )
        visual = self._normalize_visual(e.get("visual") or "")
        if visual:
            line += f"。照片里：{clip(visual, 120)}"
        else:
            line += f"，背景是{clip(e.get('desc'), 60)}"
        caption = clip(e.get("caption"), 80)
        if caption:
            line += f"（你当时配文：「{caption}」）"
        return line

    @staticmethod
    def _render_memory_injection(lines: list[str]) -> str:
        """Budget complete lines/entries, including wrapper and factuality guidance."""
        opening = (
            "<selfie_memory>\n"
            "（下面是你自己的背景资料，只是让你知道自己什么情况，不是要说给对方听的话："
            "不要主动提你在哪、在干嘛、穿什么、接下来去哪；"
            "对方问到了才回答，回答也像随口一提，别像报行程。"
            "标了「内部更新」的只当自己知道，不用复述。）\n"
        )
        closing = (
            "\n注意：照片里的人是你本人；照片细节以「照片里：」描述为准。"
            "没有照片描述时只按场景穿着回答，不编造人物或细节。"
            "\n</selfie_memory>"
        )
        kept = []
        used = len(opening) + len(closing)
        for line in lines:
            cost = len(line) + (1 if kept else 0)
            # 1500 可容纳 4 条满长度条目，保证「全局最近 4 条」完整注入
            if used + cost <= 1500:
                kept.append(line)
                used += cost
        return opening + "\n".join(kept) + closing if kept else ""

    def _memory_visible(self, entry: dict, event) -> bool:
        if bool(self._cfg("selfie_memory_cross_context", False)):
            return True
        current = str(getattr(event, "unified_msg_origin", "") or "")
        recorded = str(entry.get("umo") or "")
        return bool(current and recorded and current == recorded)

    async def _incoming_selfie_note(self, event) -> str:
        paths = await self._event_image_paths(event)
        if not paths:
            return ""
        for path in paths[:2]:
            hashes = self._img_hashes(path)
            if not hashes:
                continue
            hit = self._match_selfie(hashes)
            if hit and not self._memory_visible(hit, event):
                hit = None
            if hit:
                kind = self._note_kind(hit.get("source"))
                visual = self._normalize_visual(hit.get("visual") or "")
                if not visual:
                    # 老记录没自述：趁这次问图，现看一眼，本轮就能用上
                    visual = await self._describe_sent_image(
                        str(hit.get("path") or path), kind
                    )
                    if visual:
                        hit["visual"] = visual
                        hit["visual_ts"] = self.clock.time()
                        try:
                            self._save_state()
                        except Exception:
                            pass
                return (
                    "对方这次发来的图片是你以前发过的这张照片（可能在别的会话发过）：\n"
                    + self._format_injected_memory_entry(hit)
                    + "。照片里的人是你本人；回答以照片描述为准。"
                )
            if self._is_ref_self(hashes):
                return f"对方这次发来的图片里，人物与当前角色（{self._profile().name}）参考图匹配。"
        return ""

    async def _selfie_memory_note(self, event) -> str:
        """给模型注入「你现在在哪/穿什么/发过哪些照片、照片里有什么」的提示。"""
        if not bool(self._cfg("selfie_memory_enabled", True)):
            return ""
        lines = []
        now = self.clock.time()
        hour = int(self.clock.strftime("%H"))
        minute = int(self.clock.strftime("%M"))
        slot = self._time_slot(hour)
        where = self._scene_location_text()
        if slot == "sleep":
            bed = self._bed_subtype(hour, minute)
            if bed == "bed_sleepy":
                state_txt = f"深夜在{where}睡觉（裹着被子）"
            else:
                state_txt = f"晚上在{where}准备睡觉"
        elif slot == "home_evening":
            state_txt = f"晚上在{where}"
        else:
            state_txt = "白天在外面"
        lines.append(f"（背景：现在是 {self.clock.strftime('%H:%M')}，你{state_txt}。）")

        # 场景按整点推进：只有「缓存的场景不是这一小时」时才刷新一次，
        # 这样她才知道「这一小时换到哪了」，又不会每条消息都去查天气。
        scene = self.state.get("scene")
        if not isinstance(scene, dict):
            scene = {}
        hour_key_now = self.clock.strftime("%Y-%m-%d %H", self.clock.localtime(now))
        if str(scene.get("hour_key") or "") != hour_key_now:
            try:
                scene = await self._get_scene()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[selfie] 刷新场景失败，先用上一次的: {e!r}")
                scene = self.state.get("scene")
        if isinstance(scene, dict) and scene.get("desc"):
            scene = dict(scene)  # Compact only the request view, never persistent state.
            for field, limit in (("desc", 160), ("activity", 60), ("outfit", 60)):
                scene[field] = self._memory_injection_text(scene.get(field), limit)
            scene_key = str(scene.get("hour_key") or "")
            seen_key = str(self.state.get("memory_scene_seen") or "")
            switched = bool(scene_key and seen_key and scene_key != seen_key)
            if scene_key and scene_key != seen_key:
                self.state["memory_scene_seen"] = scene_key
                try:
                    self._save_state()
                except Exception:
                    pass
            same_slot = str(scene.get("slot") or "") == slot
            if switched:
                plan_txt = self._next_plan_text(hour, scene)
                tail = f"；{plan_txt}" if plan_txt else "；接下来还没定"
                lines.append(
                    f"（内部更新：你刚换了地方，现在在{scene.get('desc')}，"
                    f"{scene.get('activity') or '待着'}{tail}。"
                    "这条只有你自己知道，别主动跟对方说「我换地方了」。）"
                )
            elif same_slot:
                lines.append(
                    f"（背景：你目前在{scene.get('desc')}，"
                    f"{scene.get('activity') or '待着'}，穿{scene.get('outfit') or '平时的衣服'}。）"
                )
            else:
                lines.append(
                    f"（背景：你上一次拍自拍是在{scene.get('desc')}"
                    f"（{scene.get('activity') or '某处'}），现在时段已经变了，"
                    "别再说自己还在那儿。）"
                )
        if slot == "sleep":
            sleepwear = self.state.get("night_sleepwear") or {}
            if sleepwear.get("name"):
                lines.append(f"今晚的睡衣是：{self._memory_injection_text(sleepwear['name'], 60)}。")

        recent = []
        for e in (self.state.get("selfies") or []):
            if not isinstance(e, dict) or not e.get("ts"):
                continue
            try:
                # 全局选择：群聊、私聊发过的照片都算，私聊里也要认得群里那张
                if (0 <= now - float(e.get("ts") or 0) <= SELFIE_NOTE_RECENT_SECONDS
                        and self._memory_visible(e, event)):
                    recent.append(e)
            except Exception:
                continue
        # Newest first so the budget keeps the most relevant complete entries.
        mine = sorted(recent, key=lambda e: float(e["ts"]), reverse=True)[:4]
        for e in mine:
            if not e.get("visual"):
                self._schedule_self_note(e)

        # Current scene and the image being discussed take priority over history.
        image_note = await self._incoming_selfie_note(event)
        if image_note:
            lines.append(image_note)
        if mine:
            for index, e in enumerate(mine):
                entry = self._format_injected_memory_entry(e)
                if index == 0:
                    entry = "你最近发过的照片（由近到远，群聊和私聊都算）：\n" + entry
                lines.append(entry)
        living_note = self._living_memory_note()
        if living_note:
            lines.append(living_note)
        if len(lines) <= 1:
            return ""
        return self._render_memory_injection(lines)

    def _remember_livingmemory_target(self, event) -> None:
        """记住最近跟角色聊过天的会话，供日记同步到 LivingMemory 时算 scope。

        只存 umo 不够：LivingMemory 的 user 作用域是
        ``livingmemory:user:<平台类型名>:<发送者ID>``，而 umo 里的平台是配置 ID
        （例如角色名），平台类型名（aiocqhttp）只在事件对象上。顺带存下来，
        免得同步时把它算成 unknown。
        """
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
            if not umo:
                return
            targets = self.state.get("livingmemory_targets")
            if not isinstance(targets, dict):
                targets = self.state["livingmemory_targets"] = {}
            now = self.clock.time()
            previous = targets.get(umo)
            previous_updated = 0.0
            if isinstance(previous, dict):
                try:
                    previous_updated = float(previous.get("updated") or 0)
                except (TypeError, ValueError):
                    previous_updated = 0.0
            elif previous is not None:
                try:
                    previous_updated = float(previous or 0)
                except (TypeError, ValueError):
                    previous_updated = 0.0
            # 同一会话一小时刷新一次就够；避免每次 LLM 请求都写 state.json
            if previous_updated and now - previous_updated < 3600:
                return

            def pick(name):
                getter = getattr(event, name, None)
                try:
                    return str(getter() or "").strip() if callable(getter) else ""
                except Exception:
                    return ""

            targets[umo] = {"updated": now,
                            "platform": pick("get_platform_name"),
                            "sender_id": pick("get_sender_id")}
            if len(targets) > 10:
                def timestamp(item):
                    value = targets.get(item)
                    if isinstance(value, dict):
                        try:
                            return float(value.get("updated") or 0)
                        except (TypeError, ValueError):
                            return 0.0
                    try:
                        return float(value or 0)
                    except (TypeError, ValueError):
                        return 0.0
                for key in sorted(targets, key=timestamp)[:len(targets) - 10]:
                    targets.pop(key, None)
            self._save_state()
        except Exception as exc:
            logger.debug(f"[selfie] 记录 LivingMemory 会话失败: {exc!r}")

    @filter.on_llm_request()
    async def inject_selfie_memory(self, event: AstrMessageEvent, req) -> None:
        """每次 LLM 请求前，把「自己发过哪些照片、照片里有什么」塞进本轮上下文。"""
        self._remember_livingmemory_target(event)
        try:
            note = await self._selfie_memory_note(event)
            if not note:
                return
            # 首选 extra_user_content_parts：不会被写进历史，也不会被群聊插件重写 system_prompt 时丢掉
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None and TextPart is not None:
                for part in parts:
                    if "<selfie_memory>" in str(getattr(part, "text", "") or ""):
                        return
                parts.append(TextPart(text=note).mark_as_temp())
            else:
                current = str(getattr(req, "system_prompt", "") or "")
                if "<selfie_memory>" in current:
                    return
                req.system_prompt = (current + "\n\n" + note) if current else note
            if not getattr(self, "_selfie_memory_logged", False):
                self._selfie_memory_logged = True
                logger.info(
                    f"[selfie] 照片记忆已注入（{len(note)} 字，含照片描述），"
                    "之后每次对话都会带上"
                )
            else:
                logger.debug(f"[selfie] 已注入照片记忆（{len(note)} 字）")
        except Exception as e:
            logger.warning(f"[selfie] 注入照片记忆失败: {e!r}")

    def _use_reference(self, model: str | None = None, style: str | None = None,
                       scene: dict | None = None) -> bool:
        """这次生成会不会带参考图（OpenAI 兼容接口用 /images/edits 贴参考图）。

        scene 有值时把「这张会不会换素颜参考图」也算进去：换的那张图存在，
        就算原来的自动参考图缺了也照样走 edits 接口。
        """
        ref, face, single_ref = self._photo_refs(scene, style)
        return bool(ref or face or single_ref)

    def _event_photo_scene(self, scene: dict):
        """给事件里的每一张照片加随机变化：场景池抽一条、动作池抽一条。

        阶段里的「主场景/主动作」仍用于日程、日记和聊天背景；这里只给这一次
        拍照换一个池内场景与动作，返回 (场景副本, 抽到的动作正文)。不在事件里、
        或者事件没有池时原样返回。

        抽到的动作正文放进 ``event_action``，用来顶替默认的「伸手自拍」姿势；
        ``activity`` 只放动作的短名字，日程和照片配文都用它，避免把整段生图
        提示词发出去。
        """
        if not isinstance(scene, dict):
            return scene, ""
        event_id = str(scene.get("event_id") or "")
        if not event_id:
            return scene, ""
        # 结束后的「在附近休息」场景是独立一幕，不套用阶段的场景/动作池；
        # 提前到场的准备阶段也还没有「当前动作」，同样按主素材拍。
        phase = str(scene.get("living_phase") or "")
        if phase in ("end", "prepare"):
            return scene, ""
        # active 存在 state['living']['active']（由生活引擎写入），不是顶层 state['active']。
        active = active_event(self.state)
        if not active:
            return scene, ""
        event = active.get("event")
        if not isinstance(event, dict) or str(event.get("id")) != event_id:
            return scene, ""
        phases = event.get("phases") if isinstance(event.get("phases"), list) else []
        index = scene.get("phase_index")
        part = phases[index] if type(index) is int and 0 <= index < len(phases) else {}
        phase_name = phase_label(part)
        try:
            picked, action, label = photo_variation(event, scene.get("phase_index"), self.rng)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 事件随机池抽取失败，按主素材拍: {e!r}")
            return scene, ""
        variant = dict(scene)
        if phase_name:
            variant["phase_name"] = phase_name
        if isinstance(picked, dict):
            text = str(picked.get("text") or "").strip()
            name = str(picked.get("name") or "").strip()
            if text:
                variant["desc"] = text
                variant["scene_variant"] = name
                place = scene_label(picked)
                if place:
                    variant["place"] = place
                extra = picked.get("scene")
                if isinstance(extra, dict):
                    for key in ("type", "tags", "room"):
                        if extra.get(key):
                            variant[key] = extra[key]
        if not action:
            # 没有动作随机池的事件：用当前阶段的主动作正文，保证事件动作照样顶替自拍姿势。
            action = str((part or {}).get("action") or event.get("action") or "").strip()
            if action:
                label = action_name(event, part or event)
        if action:
            variant["event_action"] = action
            variant["activity"] = phase_name or label or str(variant.get("activity") or "")
        return variant, action

    def _photo_action(self, scene, expression, gesture, night_kind=""):
        """唯一决定「谁顶替默认自拍动作」的地方。

        优先级：事件当前动作 > 睡觉 / 吃饭（平级，本身不冲突）> 默认自拍。
        睡觉、吃饭、事件都只发这一套动作，不会再和「伸手自拍」叠在一起；
        自定义角色走同一个判断，只把 ``custom_text`` 追加到它自己的提示词后面。
        """
        scene = scene if isinstance(scene, dict) else {}
        # 事件进行中时事件动作优先：正在厂里拧螺丝，就不该按「睡觉」或「吃饭」拍。
        event_action = str(scene.get("event_action") or "").strip()
        if event_action:
            return {
                "source": "event",
                "intro": "一张 9:16 竖屏照片，记录她此刻正在做的事：",
                "framing": "",
                "pose": ("这一张的动作、机位和景别完全按下面这段来，"
                         f"不要再拍成伸手自拍：{event_action}；"),
                "face_line": "",
                "light_line": "",
                "tail": "",
                "custom_text": "这一张的动作与机位（务必照做）：" + event_action,
            }
        if night_kind:
            return sleep_photo(night_kind, expression)
        meal = meal_photo(scene, self.rng)
        if meal:
            return {
                "source": "meal",
                "intro": SELFIE_SHOT_INTRO,
                "framing": meal["framing"],
                "pose": meal["pose"],
                "face_line": f"脸在照片上方占三分之一，{expression}，眼睛直视镜头。",
                "light_line": "",
                "tail": meal["tail"],
                "custom_text": "这一张的动作与照片要求：" + meal_text(meal),
            }
        return {
            "source": "default",
            "intro": SELFIE_SHOT_INTRO,
            "framing": "近距离自拍特写（只拍到腰部以上，脸和上半身占满整张照片）：",
            "pose": ("镜头就在正前方，她朝镜头伸出一只手臂，前臂从镜头右下方斜着伸向镜头、"
                     "手掌在镜头外，照片里只有这条手臂；"
                     f"另一只手{gesture}；"),
            "face_line": f"脸在照片上方占三分之一，{expression}，眼睛直视镜头。",
            "light_line": "",
            "tail": "",
            "custom_text": "",
        }

    def _time_light_line(self, scene: dict) -> str:
        """这一张的时段/光线约束。

        生图模型默认把窗户补成白天，光靠场景描述压不住，所以按场景锁定的
        钟点单独写一条：夜里必须拍出夜色、只开暖色灯，窗外不能是白天。
        """
        try:
            return lighting_text(scene if isinstance(scene, dict) else None)
        except Exception:
            return ""

    def _selfie_prompt(self, scene: dict, model: str | None = None,
                       style: str | None = None,
                       look: dict | None = None,
                       selfie_action: dict | None = None,
                       refs: tuple | None = None) -> str:
        """按「模型类型 + 风格 + 这套衣服」生成提示词。

        - 带参考图：以参考图中的同一角色为准（脸/发型），衣服按这次解析出来的 look
        - 不带参考图：纯文字人设
        - 风格只有两种：纯二次元动漫 / 写实真人照片；用户当场点名的风格优先
        - 主人要求摘帽子/脱外套时，用 edit_note 明确让模型把参考图里的物件去掉
        - 动作只由 ``_photo_action`` 决定：睡觉 / 吃饭 / 事件动作都会顶替默认自拍姿势
        - ``selfie_action`` 由生图测试显式指定（名字/表情/手势），日常自拍传 None 随机抽
        """
        profile = self._profile()
        scene = scene if isinstance(scene, dict) else {}
        # 吃饭 / 做饭这一张：把菜品库里的描述补进场景，供动作和照片要求引用。
        if str(scene.get("living_phase") or "") in ("cook", "lunch", "lunch_out"):
            dish = str(scene.get("meal") or "").strip()
            info = self._meal_library().get(dish) if dish else None
            if info and info.get("description"):
                scene = dict(scene)
                scene["meal_desc"] = str(info["description"])
        if look is None:
            look = self._resolve_look(scene, style=style)
        night_kind = self._night_scene_kind(scene)  # "" / "bed" / "bunk" / "cyber"
        sleepy = str(scene.get("type") or "") == "bed_sleepy"
        # 日常自拍的表情和手势从自拍动作库随机抽一整条（15 条内置，可在「添加素材」里扩充）
        picked = (dict(selfie_action) if isinstance(selfie_action, dict) and selfie_action
                  else dict(self.rng.choice(self._selfie_actions() or SELFIE_ACTIONS)))
        expression = str(picked.get("expression") or "").strip() or self.rng.choice(EXPRESSIONS)
        if sleepy or night_kind == "cyber":
            expression = self.rng.choice(SLEEPY_EXPRESSIONS)
        gesture = str(picked.get("gesture") or "").strip() or self.rng.choice(GESTURES)
        action = self._photo_action(scene, expression, gesture, night_kind)
        use_ref = self._use_reference(model, style, scene)
        style = self._style(style)
        style_block = STYLE_BLOCK[style]
        if refs is None:
            ref_override, face_override, _single_ref = self._photo_refs(scene, style, look)
        else:
            # 生图测试显式指定参考图（参考图衣服 / 素颜勾选）：提示词和实际贴图保持一致
            ref_override, face_override, _single_ref = refs
            use_ref = bool(ref_override or face_override or _single_ref)
        wear = self._wear_text(look)
        if look.get("ref_outfit"):
            # 参考图决定衣服：提示词里只写「以参考图为准」，不再描述旧衣服
            wear = self._ref_outfit_wear(look)
        hat = str(look.get("hat") or "")
        necklace = "，脖子上戴着细项链" if look.get("necklace") else ""
        edit_note = str(look.get("edit_note") or "")
        no_hat = str(look.get("no_hat_clause") or "头部保持参考图里的样子：帽子、发型和发色都不要改")
        # 睡觉房间换了素颜脸部参考图：顺带锁一下妆容，别又给补成浓妆
        bare_face_note = ""
        if use_ref and face_override and face_override == self._ref_sleep_face_image():
            bare_face_note = ("参考图里的脸是她卸妆后的素颜（没有眼影和口红），"
                              "这张照片里也不要有浓妆，只保留自然的皮肤质感；")
        restyle_note = ""
        if use_ref and look.get("restyle"):
            restyle_note = "参考图里的衣服不要照抄，人物的衣服必须按下面这段重新拍："
        framing = action["framing"]
        pose = action["pose"]
        face_line = action["face_line"]
        light_line = action["light_line"]
        shot_intro = action["intro"]
        if use_ref:
            if style == STYLE_REALISTIC:
                ref_intro = (
                    "参考图里是同一位成年女性（真人照片，含正面三视图和细节图），"
                    "请以参考图里的这位成年女性为准"
                )
                same_person = "脸和发型保持参考图里同一个人（真人五官，不要二次元化）"
            else:
                ref_intro = "参考图里是一位成年女性角色（二次元），请以参考图中的同一个角色为准"
                same_person = "脸和发型保持参考图里同一个角色"
            head_clause = (
                f"角色外观必须和参考图完全一致：戴{hat}；" if hat
                else f"角色外观必须和参考图完全一致：{no_hat}；"
            )
            head = (
                f"{ref_intro}，"
                f"{shot_intro}"
                f"{framing}"
                f"{style_block}"
                f"{head_clause}"
                f"{restyle_note}"
                f"穿{wear}{necklace}；"
                f"{edit_note}{bare_face_note}"
                f"{pose}{face_line}"
                f"{same_person}，衣服严格按上面这段描述来拍。"
            )
        else:
            head = (
                f"{shot_intro}"
                f"{framing}"
                f"{style_block}"
                f"{pose}{face_line}"
                f"人物：{self._character(look)}。"
            )
        return (
            head
            + scene_context_text(scene)
            + f"场景：{scene.get('desc', '')}，背景是真实世界的环境，"
            "有清晰的环境细节、真实材质和自然光影，背景轻微虚化。"
            # 睡觉/网吧这类动作自己写死了灯光，就不再叠时段光线，免得两句打架
            + ("" if light_line else self._time_light_line(scene))
            + action["tail"]
            + light_line
            + (self._tongue_line() if TONGUE_PAT.search(head) else "")
            + STYLE_QUALITY[style]
        )

    def _tongue_line(self) -> str:
        """提示词里出现「舌」时，从几个舌头约束写法里随机挑一个。"""
        return self.rng.choice(TONGUE_STRUCTURE_VARIANTS)

    async def _scene_adapt_prompt(self, prompt: str, scene: dict, look: dict,
                                  cue_text: str = "") -> str:
        """特写发出去之前的最后一道：让 LLM 把动作「落进」当前场景。

        要求：服装、动作、机位一个字不改，只补「靠/坐/躺/撑在哪件家具上、接触点、透视和遮挡」，
        防止出现人悬空、床桌椅和人的透视打架、背景畸形。失败/结果不合格一律退回原提示词。
        """
        if not bool(self._cfg("closeup_scene_adapt", True)):
            return prompt
        raw = str(prompt or "").strip()
        if len(raw) < 200:
            return prompt
        provider = None
        try:
            pid = self._caption_provider_id()
            if pid and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(pid)
        except Exception:
            provider = None
        if provider is None:
            try:
                provider = self.context.get_using_provider()
            except Exception:
                provider = None
        if provider is None:
            logger.info("[selfie] 场景整合：没有可用的文本模型，直接用原提示词")
            return prompt
        # The model only supplies environment data. Original prompt remains immutable.
        return await adapt_scene_prompt(prompt, scene, provider.text_chat)

    def _closeup_prompt(self, scene: dict, model: str | None = None,
                        style: str | None = None,
                        look: dict | None = None,
                        cue: dict | None = None,
                        cos_name: str = "",
                        with_char_image: bool = True,
                        kind: str = "",
                        refs: tuple | None = None) -> str:
        """特写提示词。

        和自拍共用「人设 / 这一身衣服 / 现在的场景 / 天气和时刻」，区别只在构图：
        把「手机前置摄像头自拍」换成「镜头贴近的特写（镜头外有人拿相机在拍，照片里没有手机）」，
        再加上这次从 txt 里随机抽到的那条要求（cue）。
        """
        if look is None:
            look = self._resolve_look(scene, style=style)
        use_ref = self._use_reference(model, style, scene)
        style = self._style(style)
        style_block = STYLE_BLOCK[style]
        if refs is None:
            ref_override, face_override, _single_ref = self._photo_refs(scene, style, look)
        else:
            # 生图测试显式指定参考图（参考图衣服 / 素颜勾选）：提示词和实际贴图保持一致
            ref_override, face_override, _single_ref = refs
            use_ref = bool(ref_override or face_override or _single_ref)
        cue_text = self._clean_closeup_cue((cue or {}).get("text"))
        if not cue_text:
            cue_text = "镜头贴近拍一张特写，只拍上半身或局部，人物占满整张照片"
        if cue_text != str((cue or {}).get("text") or "").strip():
            logger.info(f"[selfie] 特写要求已剔除背景/风格/比例：{cue_text[:50]}")
        # 人体结构硬性要求：出脚就锁 5 个脚趾，出手就锁 5 根手指
        structure = []
        if CLOSEUP_TOE_PAT.search(cue_text):
            structure.append(CLOSEUP_STRUCTURE_TOE)
        if CLOSEUP_HAND_PAT.search(cue_text):
            structure.append(CLOSEUP_STRUCTURE_HAND)
        structure_line = ("细节硬性要求：" + "；".join(structure) + "。") if structure else ""
        foot_front_line = CLOSEUP_STRUCTURE_FOOT_FRONT if CLOSEUP_FOOT_FRONT_PAT.search(cue_text) else ""
        tongue_line = self._tongue_line() if TONGUE_PAT.search(cue_text) else ""
        # 腰/头旋转 + 松弛自然：正面构图的条目用轻量版，其余用完整版
        relax_line = (
            CLOSEUP_RELAX_SOFT_LINE if CLOSEUP_FRONTAL_PAT.search(cue_text)
            else CLOSEUP_RELAX_LINE
        )
        # 角度优先：点名了仰拍/俯拍就锁死，别让「特写＝镜头贴近」把它稀释成平视
        if CLOSEUP_ANGLE_UP_PAT.search(cue_text):
            angle_line = CLOSEUP_ANGLE_UP_LINE
        elif CLOSEUP_ANGLE_DOWN_PAT.search(cue_text):
            angle_line = CLOSEUP_ANGLE_DOWN_LINE
        else:
            angle_line = ""
        wear = self._wear_text(look)
        if look.get("ref_outfit"):
            # 冬装参考图：衣服以参考图为准，不再叠黄色外套/短热裤那套提示词
            wear = self._ref_outfit_wear(look)
        hat = str(look.get("hat") or "")
        necklace = "，脖子上戴着细项链" if look.get("necklace") else ""
        edit_note = str(look.get("edit_note") or "")
        no_hat = str(look.get("no_hat_clause") or "头部保持参考图里的样子：帽子、发型和发色都不要改")
        bare_face_note = ""
        if use_ref and face_override and face_override == self._ref_sleep_face_image():
            bare_face_note = ("参考图里的脸是她卸妆后的素颜（没有眼影和口红），"
                              "这张照片里也不要有浓妆，只保留自然的皮肤质感。")
        restyle_note = ""
        if use_ref and look.get("restyle"):
            restyle_note = "参考图里的衣服不要照抄，人物的衣服必须按下面这段重新拍："
        if kind:
            # 互动特写：这类提示词的关键就是「镜头外伸进来一只手」，不能沿用「不出现手臂」那句
            verb = INTERACT_VERB.get(kind) or f"轻轻{kind}着她"
            shot = (
                f"这是一张「互动特写」照片：对方正在{verb}。镜头贴近人物、构图大胆裁切，"
                "只突出局部或半身，竖幅构图。"
                "照片里必须有一只手从镜头外伸进来（那是对方的手，正在碰她），"
                "只出现这一只手、五指根数正常、不出现第二个人影；"
                "照片里不出现手机、不出现自拍杆，当成镜头外有人近距离举着相机在拍她。"
                + INTERACT_STILL_LINE
            )
        else:
            shot = (
                "这是一张「特写」照片：镜头贴近人物、构图大胆裁切，只突出局部或半身；"
                "不是自拍——照片里不出现手机、不出现自拍杆、也不出现伸向镜头的手臂，"
                "当成镜头外有个人拿相机近距离在拍她。竖幅构图。"
            )
        hour = local_datetime(self.clock.time()).hour
        # 跟场景自己的时段走（场景可能是锁定下来的一张），别用当前时钟和场景打架
        slot = str(scene.get("slot") or "") or self._time_slot(hour)
        now_txt = {
            "out": "此刻在外面", "home_evening": "傍晚在家", "sleep": "深夜在家",
        }.get(slot, "此刻待着")
        atmosphere = scene_context_text(scene) + now_txt

        face_remind = ""
        if use_ref and cos_name:
            hair_from_character = bool(look.get("cos_hair_from_character"))
            face_lock = self._cos_face_lock(with_char_image, hair_from_character,
                                            style == STYLE_REALISTIC)
            face_remind = self._cos_face_remind(with_char_image)
            if style == STYLE_REALISTIC:
                if hair_from_character:
                    base = "角色基础形象：同一个成年女性"
                    intro = (
                        "参考图1是同一位成年女性本人（真人照片，不要二次元化），"
                        "参考图2是要 cos 的角色：服装、配饰、发型和发色都按参考图2"
                    )
                    intro_nochar = ("参考图里是同一位成年女性（真人照片），脸以参考图为准，"
                                    "发型和发色按 cos 角色")
                else:
                    base = "角色基础形象必须和参考图1完全一致：同一张真人脸、同样的发型和发色"
                    intro = (
                        "参考图1是同一位成年女性（真人照片，不要二次元化），"
                        "参考图2只是要 cos 的角色服装样式"
                    )
                    intro_nochar = "参考图里是同一位成年女性（真人照片），脸和发型以参考图里的为准"
            else:
                if hair_from_character:
                    base = "角色基础形象：同一个角色"
                    intro = ("参考图1是同一个二次元角色本人，参考图2是要 cos 的角色："
                             "服装、配饰、发型和发色都按参考图2")
                    intro_nochar = ("参考图里是同一位二次元角色，脸以参考图为准，"
                                    "发型和发色按 cos 角色")
                else:
                    base = "角色基础形象必须和参考图1完全一致：同一张脸、同样的发型和发色"
                    intro = "参考图1是同一个二次元角色，参考图2是要 cos 的角色形象图"
                    intro_nochar = "参考图里是同一位二次元角色，脸和发型以参考图里的为准"
            if not with_char_image:
                intro = intro_nochar
                if hair_from_character:
                    base = "角色基础形象：同一张脸，发型、发色和头发长度按下面这段角色描述来做"
                else:
                    base = base.replace("参考图1", "参考图里")
            if hair_from_character:
                cos_line = (
                    f"这是一次特写 cosplay：她 cosplay 成《{cos_name}》，"
                    f"只穿这个角色的服装和配饰：{wear}；"
                    "发型、发色和头发长度也按这个角色来，不能保留她平时自己的头发。"
                )
            else:
                cos_line = (
                    f"这是一次特写 cosplay：她 cosplay 成《{cos_name}》，"
                    f"只穿这个角色的服装和配饰：{wear}；"
                    "脸、发色和发型仍然是她本人，不能换脸也不能换发色。"
                )
            head = (
                f"{intro}。{face_lock}{shot}{style_block}"
                f"{base}，{no_hat}；{restyle_note}{edit_note}"
                f"{cos_line}"
            )
        elif use_ref:
            if style == STYLE_REALISTIC:
                intro = (
                    "参考图里是同一位成年女性（真人照片，含正面三视图和细节图），"
                    "请以参考图里的这位成年女性为准"
                )
                same_person = "脸和发型保持参考图里同一个人（真人五官，不要二次元化）"
            else:
                intro = "参考图里是一位成年女性角色（二次元），请以参考图中的同一个角色为准"
                same_person = "脸和发型保持参考图里同一个角色"
            head_clause = (
                f"角色外观必须和参考图完全一致：戴{hat}；" if hat
                else f"角色外观必须和参考图完全一致：{no_hat}；"
            )
            head = (
                f"{intro}。{shot}{style_block}"
                f"{head_clause}{restyle_note}穿{wear}{necklace}；{edit_note}{bare_face_note}"
                f"{same_person}，衣服严格按上面这段描述来拍。"
            )
        else:
            head = f"{shot}{style_block}人物：{self._character(look)}。"

        return (
            head
            + f"这次特写的具体要求（务必照做）：{cue_text}。"
            + angle_line
            + structure_line
            + foot_front_line
            + tongue_line
            + relax_line
            + CLOSEUP_ANATOMY_LINE
            + f"场景与氛围（以这段为准）：她现在在{scene.get('desc', '')}；{atmosphere}。"
            + self._time_light_line(scene)
            + "背景就拍她此刻所在的这个真实环境，要有清晰的环境细节；"
            + "不要换成其它地方，也不要拍成纯色背景、棚拍背景或看不出环境的背景。"
            + STYLE_QUALITY[style]
            + self._scene_hard_note(scene)
            + face_remind
        )

    # ------------------------------------------------------------------
    # 生图
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # 参考图：全部来自工作台「参考图」页，不再内置任何默认参考图
    # ------------------------------------------------------------------
    def _ref_slots(self) -> dict:
        """参考图页填好的槽位 → 文件路径；没配或读失败时返回空字典。"""
        try:
            library = self._ref_library()
            if not library.configured():
                return {}
            return library.slot_paths()
        except Exception as e:  # noqa: BLE001 - 参考图库坏了不能拖垮拍照
            logger.warning(f"[selfie] 读取参考图槽位失败: {e!r}")
            return {}

    def _reference_ready(self) -> bool:
        """主站或副站至少填了一张参考图（没有参考图就不该出图）。"""
        return any(self._ref_slots().values())

    def _head_only_refs(self, scene: dict | None = None,
                        style: str | None = None) -> tuple | None:
        """换装时的头部参考图：只给脸和头发，不给穿参考图衣服的全身照。

        - 睡觉房间的素颜生效时：用参考图页上传的素颜三视图；
        - 其它：优先用参考图页的「主站参考图2」（头部三视图），没填就用主站参考图1。
        返回 (主参考图, None, None)；None 表示参考图页还没填图。
        """
        if self._style(style) != STYLE_REALISTIC:
            return None
        if self._bare_face_ref_active(scene, style):
            face = self._ref_sleep_face_image()
            return (face, None, None) if face else None
        slots = self._ref_slots()
        head = slots.get("main2") or slots.get("main1") or ""
        return (head, None, None) if head else None

    def _restyle_head_only(self, scene: dict | None, style: str | None,
                           look: dict | None) -> bool:
        """这次是不是「换装」：衣服以提示词为准，参考图只贴头部。"""
        return bool(isinstance(look, dict) and look.get("restyle")
                    and self._head_only_refs(scene, style))

    # ------------------------------------------------------------------
    # 夜间素颜：22:00~次日 07:00 在室内睡觉房间时换素颜脸部参考图
    #   启用开关和照片都在工作台「参考图」页设置，不再占配置项
    # ------------------------------------------------------------------
    def _ref_suyan_path(self) -> str:
        """参考图页启用的那张素颜三视图；没启用 / 文件缺失时返回空串。"""
        try:
            return self._ref_library().suyan_path() or ""
        except Exception as e:  # noqa: BLE001 - 参考图库坏了不能拖垮拍照
            logger.warning(f"[selfie] 读取夜间素颜参考图失败: {e!r}")
            return ""

    def _ref_sleep_face_image(self) -> str:
        """夜里睡觉房间要贴的素颜脸部参考图（参考图页「启用素颜」时才有）。"""
        return self._ref_suyan_path()

    def _ref_day_record(self) -> dict:
        record = self.state.get("ref_day") if isinstance(getattr(self, "state", None), dict) else None
        return dict(record) if isinstance(record, dict) else {}

    def _today_ref_date(self) -> str:
        """中国本地日期：跨城旅行时也沿用同一份「今天」的判定。"""
        try:
            return str(self._world_strftime("%Y-%m-%d"))
        except Exception:
            try:
                return self.clock.strftime("%Y-%m-%d", self.clock.localtime())
            except Exception:
                return ""

    def _yesterday_ref_date(self) -> str:
        try:
            return str(self._world_strftime("%Y-%m-%d", self.clock.time() - 86400))
        except Exception:
            return ""

    def _bare_face_ref_active(self, scene: dict | None = None,
                              style: str | None = None) -> bool:
        """22 点后在室内睡觉房间（网吧包厢、事件场景不算）换素颜脸部参考图。"""
        if self._style(style) != STYLE_REALISTIC:
            return False
        if not self._ref_sleep_face_image():
            return False
        try:
            hour = scene_hour(scene) if isinstance(scene, dict) else None
        except Exception:
            hour = None
        return ref_swap.bare_face_active(scene, hour)

    def _legacy_photo_refs(self, scene: dict | None = None,
                           style: str | None = None,
                           look: dict | None = None) -> tuple[str | None, str | None, str | None]:
        """原有规则算出来的（人物参考图, 脸部参考图, 单图站点人物参考图）。

        「换装」时（点单换装 / 随机衣服特写 / 事件服装 / 睡衣等）衣服由提示词决定，
        参考图只贴头部；夜里在睡觉房间且参考图页启用了素颜时，脸部参考图换成那张。
        """
        if isinstance(look, dict) and look.get("restyle"):
            head = self._head_only_refs(scene, style)
            if head:
                logger.debug("[selfie] 换装：参考图只贴头部，不贴参考图里的衣服")
                return head
        face = self._ref_sleep_face_image() if self._bare_face_ref_active(scene, style) else None
        return None, (face or None), None

    # ---------------- 工作台「参考图」页 ----------------
    def _ref_library(self):
        """参考图库实例（工作台「参考图」页上传的图片、槽位和条件）。"""
        root = os.path.abspath(os.path.join(
            str(getattr(self, "data_dir", "") or self.plugin_dir), "reference_images"))
        library = getattr(self, "_ref_store", None)
        if library is None or os.path.abspath(str(getattr(library, "root", ""))) != root:
            library = self._ref_store = ref_library.ReferenceLibrary(root)
        return library

    def _ref_rolls_today(self) -> dict:
        """今天「概率条件」的摇奖结果（7 点摇一次，一天沿用）。"""
        record = self.state.get("ref_roll") if isinstance(getattr(self, "state", None), dict) else None
        if not isinstance(record, dict) or record.get("date") != self._today_ref_date():
            return {}
        rolls = record.get("rolls")
        return dict(rolls) if isinstance(rolls, dict) else {}

    def _ensure_ref_roll(self) -> dict:
        """每天早上 7 点给「概率条件」摇一次，摇中就整天穿那一张；7 点前沿用前一天。"""
        try:
            conditions = [item for item in self._ref_library().document()["conditions"]
                          if item.get("type") == "probability"]
        except Exception as e:  # noqa: BLE001 - 参考图库坏了不能拖垮拍照
            logger.warning(f"[selfie] 读取概率参考图条件失败: {e!r}")
            return {}
        if not conditions:
            return {}
        today = self._today_ref_date()
        if not today:
            return {}
        record = self.state.get("ref_roll")
        if isinstance(record, dict) and record.get("date") == today and record.get("final"):
            return self._ref_rolls_today()
        try:
            hour = int(self._world_localtime().tm_hour)
        except Exception:
            hour = 12
        if (hour < 7 and isinstance(record, dict)
                and record.get("date") == self._yesterday_ref_date()):
            carried = {"date": today, "rolls": dict(record.get("rolls") or {}),
                       "final": False, "carried": True}
            self.state["ref_roll"] = carried
            return dict(carried["rolls"])
        rolls = {}
        for condition in conditions:
            try:
                percent = float(condition.get("percent") or 0)
            except (TypeError, ValueError):
                percent = 0.0
            rolls[str(condition.get("id") or "")] = self.rng.random() * 100.0 < percent
        self.state["ref_roll"] = {
            "date": today, "rolls": rolls, "final": True,
            "checked_at": self._world_local_datetime(self.clock.time()).isoformat(),
        }
        self._save_state()
        logger.info(
            "[selfie] 今日概率参考图判定："
            + "、".join(f"{cid}={'用' if hit else '不用'}" for cid, hit in rolls.items())
        )
        return rolls

    def _ref_today(self):
        """今天的日期对象（日期条件用）；取不到返回 None。"""
        today = self._today_ref_date()
        try:
            return datetime.strptime(today, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None

    def _ref_plan(self, scene: dict | None = None, style: str | None = None,
                  look: dict | None = None) -> dict:
        """这次出图每个站点要贴的参考图（全部来自工作台「参考图」页）。

        优先级（从高到低）：条件参考图 → 换装只贴头部（夜里在睡觉房间时头部换成
        素颜三视图，先于页面槽位）→ 页面槽位 → 夜间素颜。
        例外：夜间素颜生效时（22:00~次日 07:00 的室内睡觉房间）条件参考图整条跳过，
        素颜反过来压过条件——冬装/节日衣服不该套在床上的睡衣上，条件里的脸部参考图
        也会把刚卸完妆的脸顶回化妆脸。
        返回 ``{"main": (...), "backup": (...), "explicit": bool, "ref", "face_ref",
        "single_ref", "head_only", "condition"}``；``explicit`` 为真表示要让
        image_transport 按站点直接贴这些图（没有配置时按单站点兜底）。
        """
        legacy_ref, legacy_face, legacy_single = self._legacy_photo_refs(scene, style, look)
        bare_face = self._bare_face_ref_active(scene, style)
        plan = {
            "main": (), "backup": (), "explicit": False,
            "ref": legacy_ref, "face_ref": legacy_face, "single_ref": legacy_single,
            "head_only": self._restyle_head_only(scene, style, look),
            "condition": None, "condition_slot": "",
        }
        try:
            library = self._ref_library()
            configured = library.configured()
            slots = library.slot_paths() if configured else {}
            picked = {}
            rows = library.condition_rows() if configured else []
            if configured and not bare_face:
                picked = ref_library.resolve_conditions(
                    rows, today=self._ref_today(),
                    mean=self._ref_day_record().get("mean"), rolls=self._ref_rolls_today())
            elif rows:
                logger.debug("[selfie] 夜间睡觉房间：条件参考图这晚不换，脸部按素颜三视图拍")
        except Exception as e:  # noqa: BLE001 - 参考图库异常时退回原有规则
            logger.warning(f"[selfie] 读取参考图库失败，按原有规则挑图: {e!r}")
            configured, slots, picked = False, {}, {}

        def condition_path(slot):
            return str((picked.get(slot) or {}).get("path") or "")

        if not configured:
            return plan
        plan["explicit"] = True
        # 换装：衣服由提示词决定，只贴头部（主站参考图2 > 主站参考图1）。
        restyle = bool(isinstance(look, dict) and look.get("restyle"))
        head_refs = self._head_only_refs(scene, style)
        if restyle and head_refs:
            body_override = condition_path("main1")
            if body_override:
                plan["main"] = (body_override, condition_path("main2") or "")
                plan["head_only"] = False
                plan["condition"] = picked.get("main1")
                plan["condition_slot"] = "main1"
                plan["backup"] = (condition_path("backup1") or slots.get("backup1")
                                  or body_override,)
            else:
                # 夜里在睡觉房间换装：脸部参考图优先用素颜三视图，
                # 不能按「主站参考图2（白天的头部三视图）」贴图。
                suyan = legacy_ref if bare_face else ""
                primary = condition_path("main2") or suyan or slots.get("main2") \
                    or legacy_ref or (head_refs[0] if head_refs else "") or ""
                secondary = legacy_face or ""
                if secondary and secondary == primary:
                    secondary = ""
                plan["main"] = (primary, secondary)
                plan["head_only"] = True
                plan["condition"] = picked.get("main2")
                plan["condition_slot"] = "main2" if condition_path("main2") else ""
                plan["backup"] = (
                    condition_path("backup1") or suyan or slots.get("backup1") or primary,)
        else:
            plan["head_only"] = False
            main1 = condition_path("main1") or legacy_ref or slots.get("main1") \
                or slots.get("backup1") or ""
            main2 = condition_path("main2") or legacy_face or slots.get("main2") or ""
            backup1 = condition_path("backup1") or legacy_face or legacy_single \
                or slots.get("backup1") or main1
            backup2 = condition_path("backup2") or slots.get("backup2") or ""
            plan["main"] = (main1, main2)
            plan["backup"] = (backup1, backup2)
            if condition_path("main1"):
                plan["condition"] = picked.get("main1")
                plan["condition_slot"] = "main1"
            elif condition_path("main2"):
                plan["condition"] = picked.get("main2")
                plan["condition_slot"] = "main2"
        plan["ref"] = plan["main"][0] or None
        plan["face_ref"] = plan["main"][1] or None
        plan["single_ref"] = plan["backup"][0] or None
        return plan

    def _ref_condition_body_active(self, scene: dict | None = None,
                                   style: str | None = None) -> bool:
        """条件参考图换了「主站参考图1（全身）」：这套衣服以参考图为准。"""
        try:
            return self._ref_plan(scene, style)["condition_slot"] == "main1"
        except Exception:
            return False

    def _generation_refs(self, scene: dict | None = None, style: str | None = None,
                         look: dict | None = None) -> dict:
        """给 ``_generate`` 的参考图关键字参数（按站点显式给图时多传 main/backup）。"""
        plan = self._ref_plan(scene, style, look)
        refs = {
            "ref": plan.get("ref"), "face_ref": plan.get("face_ref"),
            "single_ref": plan.get("single_ref"), "head_only": bool(plan.get("head_only")),
        }
        if plan.get("explicit"):
            refs["main_refs"] = tuple(x for x in plan.get("main") or () if x)
            refs["backup_refs"] = tuple(x for x in plan.get("backup") or () if x)
        return refs

    def _photo_refs(self, scene: dict | None = None,
                    style: str | None = None,
                    look: dict | None = None) -> tuple[str | None, str | None, str | None]:
        """这次出图的（人物参考图, 脸部参考图, 单图站点人物参考图）；None = 自动挑。"""
        plan = self._ref_plan(scene, style, look)
        return plan.get("ref") or None, plan.get("face_ref") or None, \
            plan.get("single_ref") or None

    def _ref_outfit_wear(self, look: dict | None) -> str:
        """参考图决定衣服时，提示词里的穿着短句（整套衣服以参考图为准）。"""
        custom = str((look or {}).get("ref_outfit_text") or "")
        if custom:
            return custom
        return CONDITION_REF_OUTFIT_TEXT

    def _workspace_preview_refs(self, *, bare_face: bool = False,
                                style: str | None = None,
                                ref_outfit: dict | None = None) -> tuple | None:
        """生图测试的参考图覆盖；None = 交给当天的规则自动挑。

        - 「素颜」勾选：强制用参考图页启用的那张素颜三视图（没启用就报错）；
        - 选了工作台里起过名字的衣服参考图：主站和副站都贴那张图。
        这些只在写实风格下生效。
        """
        if self._style(style) != STYLE_REALISTIC:
            return None
        outfit = str((ref_outfit or {}).get("path") or "") if isinstance(ref_outfit, dict) else ""
        if outfit:
            face = self._ref_sleep_face_image() if bare_face else None
            return (outfit, face or None, outfit)
        if not bare_face:
            return None
        face = self._ref_sleep_face_image()
        if not face:
            raise RuntimeError(
                "参考图页还没有启用素颜：请到「参考图」页勾上「启用素颜」"
                "并上传一张素颜三视图")
        return None, (face or None), None

    async def _daily_mean_temp(self, city: str, district: str = ""):
        """查一次所在城市当日平均气温；没坐标、网络失败都返回 None。"""
        try:
            name = str(district or "").strip() or self._world_profile().home_district
            lat = lon = None
            catalog = self._city_catalog()
            if catalog:
                try:
                    lat, lon = catalog.coords(city, name)
                except Exception:
                    lat = lon = None
            if lat is None or lon is None:
                coords = self._district_coords(name)
                if coords:
                    lat, lon = coords
            if lat is None or lon is None:
                raise ValueError("该城市还没有可用坐标")
            fetcher = getattr(self, "daily_weather_fetcher", None) or fetch_daily_mean
            return parse_daily_mean(await fetcher(lat, lon))
        except Exception as e:
            logger.warning(f"[selfie] {city or '当前城市'}当日平均气温查询失败: {e!r}")
            return None

    def _ref_has_temp_conditions(self) -> bool:
        """参考图页有没有配「当日平均气温」条件；没有就不用去查天气。"""
        try:
            conditions = self._ref_library().document()["conditions"]
        except Exception:  # noqa: BLE001 - 参考图库坏了不能拖垮拍照
            return False
        return any(isinstance(item, dict) and item.get("type") == "temp"
                   for item in conditions)

    async def _ensure_ref_day(self, scene: dict | None = None) -> dict:
        """每天 7 点给概率条件摇一次，并把当日平均气温记下来给气温条件用。

        - 概率条件：7 点摇一次，摇中整天沿用，7 点前沿用前一天的结论；
        - 气温条件：用所在城市的当日平均气温（Open-Meteo `daily.temperature_2m_mean`），
          一天查一次、跨城不重判；查不到就先不触发，半小时后再试；
        - 没配气温条件就不查天气，不白发请求。
        """
        try:
            self._ensure_ref_roll()
        except Exception as e:  # noqa: BLE001 - 概率摇奖坏了不能拖垮拍照
            logger.warning(f"[selfie] 概率参考图判定失败: {e!r}")
        if not self._ref_has_temp_conditions():
            return {}
        today = self._today_ref_date()
        if not today:
            return {}
        now = self.clock.time()
        record = self._ref_day_record()
        if record.get("date") == today and record.get("final"):
            return record
        try:
            retry_at = float(record.get("retry_at") or 0)
        except (TypeError, ValueError):
            retry_at = 0
        if retry_at and now < retry_at:
            return record
        hour = int(self._world_localtime().tm_hour)
        if hour < 7 and record.get("date") == self._yesterday_ref_date() and "mean" in record:
            carried = {
                "date": today, "city": record.get("city"), "mean": record.get("mean"),
                "final": False, "carried": True,
            }
            self.state["ref_day"] = carried
            return carried
        scene = scene if isinstance(scene, dict) else {}
        city = str(scene.get("city") or self._current_city() or "")
        district = str(scene.get("district") or "")
        mean = await self._daily_mean_temp(city, district)
        if mean is None:
            failed = {"date": today, "city": city, "mean": None,
                      "final": False, "failed": True, "retry_at": now + 1800}
            self.state["ref_day"] = failed
            return failed
        record = {"date": today, "city": city, "mean": mean,
                  "final": True, "carried": False,
                  "checked_at": self._world_local_datetime(now).isoformat()}
        self.state["ref_day"] = record
        self._save_state()
        logger.info(f"[selfie] 今日气温条件判定：{city}当日平均 {mean:g}℃")
        return record



    # ------------------------------------------------------------------
    # 用户请求自拍
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 学习：从参考图学衣服 / 学造型
    # ------------------------------------------------------------------
    def _learn_pending_for(self, event, sender: str) -> dict:
        """取当前会话的学习状态；过期或换了人就当作没有。"""
        pending = self.state.get("learn_pending")
        if not isinstance(pending, dict) or not pending:
            return {}
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if pending.get("umo") != umo or str(pending.get("sender")) != str(sender):
            return {}
        if self.clock.time() - float(pending.get("ts") or 0) > LEARN_PENDING_TTL:
            self.state["learn_pending"] = {}
            self._save_state()
            return {}
        return pending

    def _clear_learn_pending(self) -> None:
        self.state["learn_pending"] = {}
        self._save_state()

    async def _handle_learn_flow(self, event, text: str, sender: str, is_admin: bool) -> bool:
        """学衣服 / 学造型：要图 -> 提取 -> 入库 -> 试拍 -> 是/不是 确认。"""
        if not bool(self._cfg("learn_enabled", True)):
            return False
        pending = self._learn_pending_for(event, sender)
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        # 1) 正在等参考图：这条消息带图就开工
        if pending and not pending.get("await_confirm"):
            paths = await self._event_image_paths(event)
            if paths:
                if self._generating:
                    await event.send(MessageChain([Plain("等我拍完这张嘛～")]))
                    return True
                self._generating = True
                self._spawn_task(
                    self._learn_from_image(event, pending, paths[0], sender, is_admin)
                )
                return True
            if text and LEARN_CANCEL_PAT.search(text):
                self._clear_learn_pending()
                await event.send(MessageChain([Plain("好，那这次先不学了～")]))
                return True
            if text and LEARN_VERB_PAT.search(text):
                await event.send(MessageChain([Plain("嗯，我在等你的参考图哦，直接发图就行～")]))
                return True
            return False

        # 2) 正在等「是不是想要的效果」
        if pending and pending.get("await_confirm"):
            if not text:
                return False
            if LEARN_VERB_PAT.search(text) and (
                LEARN_COSTUME_PAT.search(text) or LEARN_POSE_PAT.search(text)
            ):
                await event.send(MessageChain([
                    Plain("先回我「是」或「不是」嘛，这张还没确认呢～")
                ]))
                return True
            core = re.sub(r"[吧呢啊嘛呀哦的啦~。!?，,.\s]+$", "", text.strip())
            if COS_NO_PAT.search(core) or core in ("不", "否"):
                await self._learn_rollback(event, pending)
                return True
            if COS_YES_PAT.match(core):
                label = str(pending.get("label") or "刚学的那条")
                self._clear_learn_pending()
                await event.send(MessageChain([Plain(f"好～{label}我收下了。")]))
                return True
            return False

        # 3) 新的学习请求
        if not text or not LEARN_VERB_PAT.search(text):
            return False
        if not self._can_trigger(event, text):
            return False
        want_costume = bool(LEARN_COSTUME_PAT.search(text))
        want_pose = bool(LEARN_POSE_PAT.search(text))
        if not (want_costume or want_pose):
            return False
        if (not is_admin) and bool(self._cfg("learn_admin_only", True)):
            await event.send(MessageChain([Plain("学习衣服和造型只有主人能用哦～")]))
            return True
        if self._generating:
            await event.send(MessageChain([Plain("等我拍完这张嘛～")]))
            return True
        kind = "costume" if want_costume else "pose"
        self.state["learn_pending"] = {
            "umo": umo, "sender": str(sender), "kind": kind,
            "ts": self.clock.time(), "await_confirm": False,
        }
        self._save_state()
        await event.send(MessageChain([
            Plain(LEARN_ASK_COSTUME if kind == "costume" else LEARN_ASK_POSE)
        ]))
        logger.info(f"[selfie] 进入学习流程：kind={kind}, sender={sender}")
        return True

    async def _learn_from_image(self, event, pending: dict, path: str,
                                sender: str, is_admin: bool) -> None:
        """看参考图 -> 提取提示词 -> 写进衣柜/特写池 -> 按新条目出一张 -> 等确认。"""
        kind = str(pending.get("kind") or "costume")
        umo = str(pending.get("umo") or "")
        try:
            await event.send(MessageChain([
                Plain("我看一眼这张图，把"
                      + ("衣服" if kind == "costume" else "造型")
                      + "整理成提示词～")
            ]))
            if kind == "costume":
                info = await self._vision_extract_costume(path)
                if not info:
                    self._clear_learn_pending()
                    await event.send(MessageChain([
                        Plain("这张图我没看明白……换一张清楚点、能看到整身衣服的？")
                    ]))
                    return
                name, aliases, desc = info
                if not self._append_costume_to_file(name, aliases, desc):
                    self._clear_learn_pending()
                    await event.send(MessageChain([Plain("衣柜里写不进去，等会儿再试？")]))
                    return
                # append clears this field before every attempt; never reuse an older line.
                line = getattr(self, "_last_appended_costume_line", "")
                if not line:
                    self._clear_learn_pending()
                    await event.send(MessageChain([Plain("衣服已写入，但无法记录回滚信息，请检查衣柜文件。")]))
                    return
                self.state["learn_pending"] = {
                    "umo": umo, "sender": str(sender), "kind": "costume",
                    "ts": self.clock.time(), "await_confirm": True,
                    "label": f"衣服「{name}」", "line": line, "target": name,
                }
                self._save_state()
                await event.send(MessageChain([Plain(f"学到一套「{name}」，我穿上试一张～")]))
                ok = await self._do_selfie(
                    event, is_admin, self._user_style(sender), {}, sender,
                    {"name": name, "desc": desc}, True,
                )
                if not ok:
                    self._remove_learn_entry("costume", line)
                    self._clear_learn_pending()
                    await event.send(MessageChain([
                        Plain("试穿这张没拍出来，这套先不收了，等会儿重新教我一次吧～")
                    ]))
                    return
                question = (
                    f"穿上是这个效果吗？是的话我就把「{name}」收进衣柜；"
                    "不是的话回「不是」，我把这套删掉。"
                )
            else:
                cue_text = await self._vision_extract_pose(path)
                if not cue_text:
                    self._clear_learn_pending()
                    await event.send(MessageChain([
                        Plain("这张图的姿势我没整理明白……换一张构图清楚、人物完整的？")
                    ]))
                    return
                added = self._append_closeup_prompt(cue_text)
                if not added:
                    self._clear_learn_pending()
                    await event.send(MessageChain([Plain("特写池写不进去，等会儿再试？")]))
                    return
                num, line = added
                total = len(self._closeup_prompts())
                self.state["learn_pending"] = {
                    "umo": umo, "sender": str(sender), "kind": "pose",
                    "ts": self.clock.time(), "await_confirm": True,
                    "label": f"特写第 {num} 条", "line": line, "target": str(num),
                }
                self._save_state()
                await event.send(MessageChain([
                    Plain(f"第 {num} 条进特写池了（{len(cue_text)} 字，一共 {total} 条），我按这个造型拍一张～")
                ]))
                ok = await self._do_closeup(
                    event, is_admin, sender, self._user_style(sender), None, None,
                    {"id": num, "text": cue_text}, None,
                )
                if not ok:
                    self._remove_learn_entry("pose", line)
                    self._clear_learn_pending()
                    await event.send(MessageChain([
                        Plain("按这个造型没拍出来，这条先删掉了，等会儿再教我一次吧～")
                    ]))
                    return
                question = (
                    f"是这个感觉吗？是的话我就留着（特写池第 {num} 条）；"
                    "不是的话回「不是」，我把这条删掉。"
                )
            await event.send(MessageChain([Plain(question)]))
            logger.info(f"[selfie] 学习流程产出一版：kind={kind}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[selfie] 学习流程失败: {e!r}")
            try:
                await event.send(MessageChain([Plain("刚才没整理成功，等会儿再试？")]))
            except Exception:
                pass
            self._clear_learn_pending()
        finally:
            self._generating = False

    async def _learn_rollback(self, event, pending: dict) -> None:
        """用户说「不是」：把刚写进去的那一行删掉。"""
        kind = str(pending.get("kind") or "costume")
        line = str(pending.get("line") or "")
        label = str(pending.get("label") or ("这套衣服" if kind == "costume" else "这条特写"))
        removed = self._remove_learn_entry(kind, line)
        self._clear_learn_pending()
        if removed:
            await event.send(MessageChain([Plain(f"好，{label}我删掉啦～")]))
        else:
            await event.send(MessageChain([Plain(f"好，{label}没留在库里，不用删～")]))

    def _remove_learn_entry(self, kind: str, line: str) -> bool:
        if not line:
            return False
        if kind == "costume":
            ok = self._remove_exact_line(self._costumes_path(), line)
            self._costume_cache = None
            return ok
        ok = self._remove_exact_line(self._closeup_prompt_path(), line)
        self._closeup_cache = None
        return ok

    @staticmethod
    def _remove_exact_line(path: str, line: str) -> bool:
        """按内容精确删掉 txt 里的一行（保留原文件的换行风格与 BOM）。"""
        path = str(path or "")
        target = str(line or "").strip()
        if not path or not target or not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                raw = f.read()
            bom = raw.startswith(b"\xef\xbb\xbf")
            body = raw[3:] if bom else raw
            text = body.decode("utf-8", "replace")
            crlf = "\r\n" in text
            lines = text.replace("\r\n", "\n").split("\n")
            kept = [ln for ln in lines if ln.strip() != target]
            if len(kept) == len(lines):
                return False
            out = "\n".join(kept)
            if not out.endswith("\n"):
                out += "\n"
            if crlf:
                out = out.replace("\n", "\r\n")
            data = out.encode("utf-8")
            with open(path, "wb") as f:
                f.write((b"\xef\xbb\xbf" + data) if bom else data)
            logger.info(f"[selfie] 学习回滚：从 {os.path.basename(path)} 删掉一行")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 删除学习条目失败（{path}）: {e!r}")
            return False

    def _append_closeup_prompt(self, cue_text: str, path: str = "") -> tuple[int, str] | None:
        """把学到的一条特写追加进 closeup_prompts.txt（编号取现有最大号 +1）。"""
        p = str(path or self._closeup_prompt_path() or "")
        if not p:
            return None
        text = self._clean_one_line(cue_text, 1200)
        if not text:
            return None
        if len(text) < CLOSEUP_CUE_MIN_LEN:
            logger.warning(
                f"[selfie] 新特写提示词只有 {len(text)} 字，"
                f"低于 {CLOSEUP_CUE_MIN_LEN} 字底线，动作可能抓不准"
            )
        try:
            backup_dir = os.path.join(self.data_dir, "material_backups") if getattr(self, "data_dir", None) else None
            store = MaterialStore(self._material_dir(), {"closeup": os.path.abspath(p)},
                                  backup_dir=backup_dir, fallback_root=self.plugin_dir)
            draft = store.create_draft("closeup", text)
            entry = store.confirm_draft(draft["token"])
            num = entry["id"]
            line = f"{num}：{entry['text']}"
            self._closeup_cache = None
            logger.info(f"[selfie] 特写池新增第 {num} 条：{text[:40]}")
            return num, line
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 写入 closeup_prompts.txt 失败: {e!r}")
            return None

    def _learn_vision_provider_id(self) -> str:
        pid = str(self._cfg("learn_vision_provider_id", "") or "").strip()
        return pid or self._caption_provider_id()

    async def _learn_vision_chat(self, path: str, prompt: str) -> str:
        """读一张参考图，返回模型原始文本。

        依次试：配置点名的看图模型 -> 发图自述用的看图模型 -> 当前对话模型 ->
        生图那套中转站上的视觉模型（AstrBot 的模型不吃图片时靠它兜底）。
        """
        path = str(path or "")
        if not path or not os.path.exists(path):
            return ""
        candidates = []
        pid = self._learn_vision_provider_id()
        if pid:
            candidates.append(pid)
        cap = self._caption_provider_id()
        if cap and cap not in candidates:
            candidates.append(cap)
        for cid in candidates:
            text = await self._learn_vision_via_provider(cid, path, prompt)
            if text:
                return text
        text = await self._learn_vision_via_provider("", path, prompt)
        if text:
            return text
        fallback = await self._learn_vision_via_relay(path, prompt)
        if fallback:
            logger.info("[selfie] 学习看图改用中转站视觉模型提取成功")
            return fallback
        logger.warning("[selfie] 学习看图没有可用通道，提取失败")
        return ""

    async def _learn_vision_via_provider(self, provider_id: str, path: str, prompt: str) -> str:
        provider = None
        try:
            if provider_id and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return ""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, image_urls=[path]), timeout=60,
            )
            return (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 学习看图（{provider_id or '当前模型'}）失败: {e!r}")
            return ""

    async def _learn_vision_via_relay(self, path: str, prompt: str) -> str:
        """可选兜底：用生图那套中转站（api_base/api_key）的视觉模型看图。

        默认不启用——留空就直接返回，避免悄悄产生额外费用；要兜底再在
        learn_vision_model 里填一个中转站上支持图片的模型名。
        """
        model = str(self._cfg("learn_vision_model", "") or "").strip() or DEFAULT_LEARN_VISION_MODEL
        if not model:
            return ""
        base, key = self._api()
        if not base or not key:
            return ""
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            # 思考型模型会把 token 花在 reasoning 上，给足预算，否则 content 会是空的
            payload = {
                "model": model, "max_tokens": 3000,
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}},
                ]}],
            }
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(
                timeout=timeout, headers={"User-Agent": BROWSER_UA}
            ) as session:
                async with session.post(
                    base.rstrip("/") + "/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer " + key},
                ) as resp:
                    status = resp.status
                    if status != 200:
                        logger.warning(f"[selfie] 学习看图（中转站 {model}）HTTP {status}")
                        return ""
                    data = await resp.json(content_type=None)
            item = ((data.get("choices") or [{}])[0].get("message") or {})
            return str(item.get("content") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 学习看图（中转站 {model}）异常: {e!r}")
            return ""

    async def _vision_extract_costume(self, path: str) -> tuple[str, str, str] | None:
        raw = await self._learn_vision_chat(path, LEARN_COSTUME_PROMPT)
        data = self._parse_costume_match_json(raw) if raw else None
        if not isinstance(data, dict):
            logger.warning(f"[selfie] 学衣服：模型没返回可用 JSON：{str(raw)[:120]!r}")
            return None
        name = self._clean_one_line(str(data.get("name") or ""), 12)
        aliases = self._clean_one_line(str(data.get("aliases") or ""), 60)
        desc = self._clean_one_line(str(data.get("desc") or ""), 300)
        if not desc or len(desc) < 12:
            return None
        if not name:
            name = self._clean_one_line(desc, 8)
        if not name:
            return None
        name = self._unique_costume_name(name)
        if not aliases:
            aliases = name
        return name, aliases, desc

    def _unique_costume_name(self, name: str) -> str:
        """自动起名，保证不和衣柜里已有的名字/别名撞。"""
        outfits, aliases = self._costume_tables()
        base = self._clean_one_line(name, 12) or "新衣服"
        if base not in outfits and base not in aliases:
            return base
        for i in range(2, 50):
            cand = (base + str(i))[:12]
            if cand not in outfits and cand not in aliases:
                return cand
        return (base + str(int(self.clock.time()) % 1000))[:12]

    async def _vision_extract_pose(self, path: str) -> str:
        """看图 -> 一条 500 字以上的特写提示词（太短会自动再要一次）。"""

        def _norm(raw: str) -> str:
            text = self._clean_one_line(raw, 1200)
            if not text or "【" not in text:
                return ""
            cleaned = self._clean_closeup_cue(text)
            cleaned = re.sub(r"^\s*\d+\s*[：:.、)）]\s*", "", cleaned)
            if len(cleaned) < 40:
                return ""
            if not any(k in cleaned for k in ("神态", "机位", "滤镜", "动作")):
                return ""
            return cleaned

        text = _norm(await self._learn_vision_chat(path, LEARN_POSE_PROMPT))
        if text and len(text) < CLOSEUP_CUE_MIN_LEN:
            logger.info(
                f"[selfie] 学造型第一版只有 {len(text)} 字，自动再要一次"
                f"（目标 >= {CLOSEUP_CUE_MIN_LEN} 字）"
            )
            text2 = _norm(await self._learn_vision_chat(path, LEARN_POSE_EXPAND_PROMPT))
            if len(text2) > len(text):
                text = text2
        if not text:
            return ""
        if len(text) < CLOSEUP_CUE_MIN_LEN:
            logger.warning(
                f"[selfie] 学造型最终只有 {len(text)} 字，低于 {CLOSEUP_CUE_MIN_LEN} 字底线"
            )
        else:
            logger.info(f"[selfie] 学造型拿到 {len(text)} 字")
        return text

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 5)
    async def on_selfie_request(self, event: AstrMessageEvent):
        lock = getattr(self, "_request_admission_lock", None)
        if lock is None:
            lock = self._request_admission_lock = asyncio.Lock()
        async with lock:
            return await self._dispatch_selfie_request(event)

    async def _dispatch_selfie_request(self, event: AstrMessageEvent):
        try:
            if event.get_platform_name() != "aiocqhttp":
                return
            text = self._clean_text(event)
            sender = str(event.get_sender_id())
            is_admin = sender in self._admins()
            # 学习衣服 / 学习造型：先处理「等参考图」和「等是/不是」两种状态
            # （等图那条消息可能只有图片、没有文字，必须放在 text 为空判断之前）
            if await self._handle_learn_flow(event, text, sender, is_admin):
                event.stop_event()
                return
            if not text:
                return

            # admin 用法只给管理员：非管理员带 admin 又确实在要自拍/特写/互动，直接拒绝。
            # 以前是静默忽略 admin 字样、按普通请求继续跑，看起来像「admin 谁都能用」。
            if not is_admin and ADMIN_MODE_PAT.search(text):
                looks_like_request = bool(
                    self._selfie_match(text)
                    or CLOSEUP_PAT.search(text)
                    or self._interaction_match(text)
                    or any(k in text for k in ("拍", "照片", "自拍", "特写", "换装", "cos", "cosplay"))
                )
                if looks_like_request:
                    logger.info(
                        f"[selfie] 非管理员 {sender} 尝试使用 admin 用法，已拒绝：{text[:50]}"
                    )
                    await event.send(MessageChain([
                        Plain("这个用法只有主人能用哦～想拍照直接跟我说「拍张自拍」「特写」就行")
                    ]))
                    event.stop_event()
                    return

            if is_admin and ("发空间" in text or "空间说说" in text) and (
                "自拍" in text or "照片" in text or "说说" in text
            ):
                await event.send(MessageChain([Plain("好，我去空间发一条带图说说～")]))
                self._spawn_task(self._make_qzone_post(force=True))
                event.stop_event()
                return

            # 0) cosplay 的「是不是这个角色」确认回复，先拦下来
            if await self._handle_cos_confirmation(event, text):
                event.stop_event()
                return

            # 0) 风格偏好设置：「自拍风格改为二次元 / 以后都用写实」
            pref_style, rest_text = self._detect_style_pref(text)
            if pref_style:
                if not self._can_trigger(event, text):
                    return
                self._set_user_style(sender, pref_style)
                style_name = "二次元" if pref_style == STYLE_ANIME else "真人"
                await event.send(MessageChain([
                    Plain(f"好呀，以后给你拍的照片都用{style_name}风格啦～")
                ]))
                logger.info(f"[selfie] 用户 {sender} 的风格偏好改为 {pref_style}")
                # 这句话里如果还夹着「来一张 / 拍一张 / 特写」，接着拍；否则只记设置
                if not (self._selfie_match(rest_text)
                        or CLOSEUP_PAT.search(rest_text)
                        or any(k in rest_text for k in ("拍一张", "来一张", "看看"))):
                    event.stop_event()
                    return
                text = rest_text or text

            # 0.35) 随机衣服特写：衣柜随机 + 特写随机（仅限可换装时段）
            if self._is_random_costume_closeup(text):
                if await self._handle_random_costume_closeup(event, text, sender, is_admin):
                    event.stop_event()
                return

            # 0.4) 互动：摸摸 / 捏捏（这两条提示词独立于特写 txt，随机特写摇不到）
            if self._interaction_match(text) or (
                is_admin and ADMIN_MODE_PAT.search(text) and INTERACT_ADMIN_PAT.search(text)
            ):
                if await self._handle_interact_request(event, text, sender, is_admin):
                    event.stop_event()
                return

            # 0.5) 特写：单独一条流程（不是自拍视角），场景/衣服/额度/记忆都和自拍共用
            if CLOSEUP_PAT.search(text):
                if await self._handle_closeup_request(event, text, sender, is_admin):
                    event.stop_event()
                return

            # 管理员模式：自拍也认「admin自拍。女仆装」——任意时间可换装 / cos
            admin_mode = bool(is_admin and ADMIN_MODE_PAT.search(text))
            if admin_mode:
                text = ADMIN_MODE_PAT.sub("", text, count=1).strip(" ，,。;；")
                logger.info(f"[selfie] 管理员模式（自拍）：忽略换装/ cos 时段限制，文本={text[:40]}")

            wants_selfie = bool(self._selfie_match(text))
            cos_selfie = self._is_cos_selfie(text)
            if not wants_selfie:
                return
            if not (admin_mode or self._can_trigger(event, text)):
                return
            if self._generating:
                await event.send(MessageChain([Plain("等我拍完这张嘛～")]))
                event.stop_event()
                return
            if not is_admin:
                if self._quota_left(sender) <= 0:
                    await event.send(
                        MessageChain([
                            Plain(f"今天已经拍够{self._daily_limit()}张啦，明天再给你看～")
                        ])
                    )
                    event.stop_event()
                    return
                # cosplay 是明确点单，不掷「想不想拍」的骰子；普通自拍保留概率
                if not cos_selfie:
                    try:
                        prob = float(self._cfg("selfie_probability", DEFAULT_PROB))
                    except Exception:
                        prob = DEFAULT_PROB
                    if self.rng.random() > prob:
                        await event.send(
                            MessageChain([Plain("现在不太想拍嘛，等会儿再给你看～")])
                        )
                        event.stop_event()
                        return

            # 换衣门控要用当前场景：家里/住宿任意时段可换，其它地点不行（旧实例保持原规则）
            scene = await self._get_scene()

            # 1) cosplay：先搜角色图 -> 发图确认 -> 确认后才生成
            if cos_selfie:
                if not bool(self._cfg("cos_enabled", True)):
                    await event.send(MessageChain([Plain("cosplay 我先关掉啦，先穿点别的？")]))
                    event.stop_event()
                    return
                cos_slot = self._time_slot(int(self.clock.strftime("%H")))
                cos_refuse = "" if admin_mode else self._cos_refusal(cos_slot, scene)
                if cos_refuse:
                    await event.send(MessageChain([Plain(cos_refuse)]))
                    logger.info(f"[selfie] 拒绝 cos 点单（地点={scene.get('type')}，时段={cos_slot}）")
                    event.stop_event()
                    return
                cos_name = self._detect_cos(text)
                if not cos_name:
                    await event.send(MessageChain([
                        Plain("好呀，你想让我 cos 谁呀？说个名字就行（日文名、英文名也可以）～")
                    ]))
                    event.stop_event()
                    return
                self._generating = True
                self._spawn_task(self._start_cos_request(event, sender, cos_name))
                event.stop_event()
                return

            # 2) 「点菜」换装：能换衣的地点（家/住宿）任意时段可换；换不了就明确婉拒
            style_override = self._detect_style_request(text)
            # 「摘掉帽子 / 脱掉外套」只有主人（管理员）说了才生效
            removal = self._detect_removal(text) if is_admin else {}
            # 先按场景判能不能换装；换不了就只用正则判「有没有在点衣服」，
            # 不为了一条注定要婉拒的消息去等模型
            slot = self._time_slot(int(self.clock.strftime("%H")))
            refuse = "" if admin_mode else self._costume_refusal(slot, scene)
            costume = await self._detect_costume(text, llm=not refuse)
            if costume:
                if refuse:
                    await event.send(MessageChain([Plain(refuse)]))
                    logger.info(f"[selfie] 拒绝换装点单（地点={scene.get('type')}，时段={slot}）：{costume.get('name')}")
                    event.stop_event()
                    return
            # 她的白天活动自己安排，不接受「去 xx 拍」这种点单（会在接单话里婉拒）
            scene_req = self._detect_scene_request(text)
            # 风格优先级：本句点名 > 该用户的长期偏好 > 配置默认（真人）
            eff_style = style_override or self._user_style(sender)
            await self._send_ack(event, style_override, removal, costume, scene_req)
            self._generating = True
            self._spawn_task(
                self._do_selfie(event, is_admin, eff_style, removal, sender, costume,
                                force_costume=admin_mode)
            )
            event.stop_event()
        except WorldNotReady:
            # 还没设定家城市：明确让主人去工作台配置，不静默、也不编造场景。
            try:
                await event.send(MessageChain([Plain(WorldNotReady.message)]))
            except Exception:
                pass
            event.stop_event()
        except Exception as e:
            logger.warning(f"[selfie] 处理自拍请求失败: {e}")

    @staticmethod
    def _is_random_costume_closeup(text: str) -> bool:
        """「随机衣服特写 / 随便穿一套来个特写」这类指令。"""
        if not text or not CLOSEUP_PAT.search(text):
            return False
        return bool(RANDOM_WORD_PAT.search(text) and COSTUME_WORD_PAT.search(text))

    async def _handle_random_costume_closeup(self, event: AstrMessageEvent, text: str,
                                            sender: str, is_admin: bool) -> bool:
        """随机衣服特写：探测衣柜条目数 -> roll 衣服 -> roll 特写 -> 出图。

        返回 True 表示这条消息已经被她接下。
        """
        if not bool(self._cfg("closeup_enabled", True)):
            return False
        admin_mode = bool(is_admin and ADMIN_MODE_PAT.search(text))
        if not (admin_mode or self._can_trigger(event, text)):
            return False
        slot = self._time_slot(int(self.clock.strftime("%H")))
        # 仅限能换衣的地点（家里/住宿）；管理员可以「admin随机衣服特写」任意时间测试
        refuse = "" if admin_mode else self._costume_refusal(slot, await self._get_scene())
        if refuse:
            await event.send(MessageChain([Plain(refuse)]))
            logger.info(f"[selfie] 拒绝「随机衣服特写」（时段={slot}）")
            return True
        if admin_mode:
            logger.info(f"[selfie] 管理员模式（随机衣服特写）：忽略时段限制，时段={slot}")
        if self._generating:
            await event.send(MessageChain([Plain("等我拍完这张嘛～")]))
            return True
        if not is_admin and self._quota_left(sender) <= 0:
            await event.send(MessageChain([
                Plain(f"今天已经拍够{self._daily_limit()}张啦，明天再给你看～")
            ]))
            return True

        # 1) 先探测衣柜条目数量（改过 costumes.txt 会按 mtime 自动重载）
        outfits, _ = self._costume_tables()
        total = len(outfits)
        if total <= 0:
            await event.send(MessageChain([Plain("衣柜里现在一件衣服都没有……")]))
            logger.warning("[selfie] 随机衣服特写：衣柜为空")
            return True

        # 2) 按条目数 roll 一套衣服
        keys = list(outfits.keys())
        pick_idx = random.randrange(total)
        name = keys[pick_idx]
        desc = outfits[name]
        costume = {"name": name, "desc": desc}

        # 3) 再 roll 一条特写（不会连着两次抽到同一条）
        cue = self._pick_closeup_prompt(pm=False)
        if not cue or cue.get("error"):
            await event.send(MessageChain([Plain("特写池好像有点问题，等会儿再试？")]))
            return True
        cue_total = int(cue.get("total") or 0)

        if self._natural_chat_enabled():
            ack = await self._natural_ack(
                event,
                action_line=f"用「{name}」这套衣服拍一张近距离特写",
                detail=f"你刚从衣柜里随手抽到「{name}」这套衣服，准备换上去拍一张特写（别报编号和条数）",
                fallback_pool=[f"好～我抽到「{name}」这套，等我换上去拍一张"],
            )
        else:
            ack = (
                f"好～衣柜里一共 {total} 套，随机到第 {pick_idx + 1} 套「{name}」，"
                f"再随机配第 {cue.get('id')}/{cue_total} 条特写🤳"
            )
        await self._send_reply(event, ack)
        logger.info(
            f"[selfie] 随机衣服特写：衣柜 {total} 套 -> 第 {pick_idx + 1} 套「{name}」；"
            f"特写池 {cue_total} 条 -> 第 {cue.get('id')} 条"
        )
        style_override = self._detect_style_request(text)
        eff_style = style_override or self._user_style(sender)
        self._generating = True
        self._spawn_task(self._do_closeup(
            event, is_admin, sender, eff_style, {}, costume, cue, force_costume=True,
        ))
        return True

    async def _handle_closeup_request(self, event: AstrMessageEvent, text: str,
                                      sender: str, is_admin: bool) -> bool:
        """处理「特写」点单。返回 True 表示这条消息已经被她接下（调用方 stop_event）。

        规则：
        - 任何时候都能要特写（场景/天气/衣服/睡衣和自拍一套逻辑）
        - 只有能换衣的地点（家里/住宿）才能换衣服 / cos；其它地点先婉拒，仍按既定衣服拍特写
        - 「特写 + cos」要先搜角色图、等对方确认，确认后才用「角色图 + 三视图」生成
        """
        if not bool(self._cfg("closeup_enabled", True)):
            return False
        # 管理员模式：带 admin 且是管理员 -> 任意时间可换装/ cos，还能「admin特写N」点名；
        # 而且是主人发的那就不用再 @ 或带角色名
        admin_mode = bool(is_admin and ADMIN_MODE_PAT.search(text))
        if not (admin_mode or self._can_trigger(event, text)):
            return False
        slot = self._time_slot(int(self.clock.strftime("%H")))
        forced_id = None
        if admin_mode:
            text = ADMIN_MODE_PAT.sub("", text, count=1).strip(" ，,。;；")
            m = ADMIN_CLOSEUP_ID_PAT.search(text)
            if m:
                forced_id = int(m.group(1))
                text = text.replace(m.group(0), "特写", 1)
            logger.info(
                f"[selfie] 管理员模式（特写）：时段={slot}，指定编号={forced_id}，"
                f"剩余文本={text[:40]}"
            )
        if self._generating:
            await event.send(MessageChain([Plain("等我拍完这张嘛～")]))
            return True
        if not is_admin and self._quota_left(sender) <= 0:
            await event.send(MessageChain([
                Plain(f"今天已经拍够{self._daily_limit()}张啦，明天再给你看～")
            ]))
            return True

        style_override = self._detect_style_request(text)
        # 「摘掉帽子 / 脱掉外套」只有主人（管理员）说了才生效，这点和自拍一致
        removal = self._detect_removal(text) if is_admin else {}
        explicit = False
        # 换衣门控要用当前场景：家里/住宿任意时段可换，其它地点不行（旧实例保持原规则）
        scene = await self._get_scene()
        # 「特写pm」= 特殊触发：只有它才抽得到 closeup_pm_ids 里那几条（默认 2、6）
        pm = bool(CLOSEUP_PM_PAT.search(text))

        # 1) 特写 + cos：只有能换衣的地点（家里/住宿）才进 cosplay 流程
        if COS_KEYWORD_PAT.search(text):
            explicit = True
            if not bool(self._cfg("cos_enabled", True)):
                await event.send(MessageChain([
                    Plain("cosplay 我先关掉啦，先拍张普通的特写？")
                ]))
            else:
                cos_name = self._detect_cos(text)
                cos_refuse = "" if admin_mode else self._cos_refusal(slot, scene)
                if cos_refuse:
                    await event.send(MessageChain([Plain(cos_refuse)]))
                    logger.info(f"[selfie] 拒绝特写 cos（地点={scene.get('type')}，时段={slot}），改按既定衣服拍特写")
                elif not cos_name:
                    await event.send(MessageChain([
                        Plain("好呀，你想让我 cos 谁呀？说个名字就行（日文名、英文名也可以）～")
                    ]))
                    return True
                else:
                    cue = await self._pick_closeup_cue(event, pm, forced_id, allow_sketch=False)
                    if cue is None:
                        return True
                    self._generating = True
                    self._spawn_task(self._start_cos_request(
                        event, sender, cos_name, "closeup", cue,
                    ))
                    return True

        # 2) 特写 + 换装：能换衣的地点（家里/住宿）任意时段可换；其它地点婉拒后照常拍
        costume = None
        refuse = "" if admin_mode else self._costume_refusal(slot, scene)
        if not COS_KEYWORD_PAT.search(text):
            # 换不了的地方只用正则判有没有在点衣服，别为一条注定婉拒的消息等模型
            costume = await self._detect_costume(text, llm=not refuse)
        if costume:
            explicit = True
            if refuse:
                await event.send(MessageChain([Plain(refuse)]))
                logger.info(f"[selfie] 拒绝特写换装（地点={scene.get('type')}，时段={slot}）：{costume.get('name')}")
                costume = None

        # 3) 普通特写：和普通自拍一样掷一次「想不想拍」的骰子；点单（换装/cos）不掷
        if (not explicit) and (not is_admin):
            try:
                prob = float(self._cfg("selfie_probability", DEFAULT_PROB))
            except Exception:
                prob = DEFAULT_PROB
            if self.rng.random() > prob:
                await event.send(MessageChain([Plain("现在不太想拍嘛，等会儿再给你看～")]))
                return True

        cue = await self._pick_closeup_cue(event, pm, forced_id)
        if cue is None:
            return True
        eff_style = style_override or self._user_style(sender)
        if costume:
            # 换装：统一回「我去找一件xxx，你要的是这个对吧」，方便对方一眼纠错
            if self._natural_chat_enabled():
                ack = await self._natural_ack(
                    event,
                    action_line=f"换成「{costume.get('name') or '那套衣服'}」再拍一张近距离特写",
                    detail=(
                        f"你要说出这套衣服的名字「{costume.get('name') or '那套衣服'}」，"
                        "方便对方一眼纠错"
                    ),
                    fallback_pool=[self._costume_confirm_text(costume) + "～"],
                )
            else:
                ack = self._costume_confirm_text(costume) + "～"
        elif self._natural_chat_enabled():
            ack = await self._natural_ack(
                event,
                action_line="拍一张近距离特写（镜头外相机视角）",
                fallback_pool=list(CLOSEUP_ACK_FALLBACK),
            )
        else:
            ack = self._closeup_ack(False)
        await self._send_reply(event, ack)
        self._generating = True
        self._spawn_task(self._do_closeup(
            event, is_admin, sender, eff_style, removal, costume, cue,
            force_costume=admin_mode,
        ))
        return True

    # ------------------------------------------------------------------
    # 互动模块：摸摸 / 捏捏 / 戳戳 / 揉揉 / 拍拍（提示词在 interaction.txt）
    # ------------------------------------------------------------------
    async def _handle_interact_request(self, event: AstrMessageEvent, text: str,
                                       sender: str, is_admin: bool) -> bool:
        """处理带互动触发词的语句。返回 True 表示这条消息已经被她接下。

        规则：
        - 语句里出现摸摸 / 捏捏 / 戳戳 / 揉揉 / 拍拍等触发词，就按这个词去互动库
          匹配行首动作词相同的提示词；匹配到多条就随机 roll 一条
        - 库里没有以这个触发词开头的提示词：回「有点害羞 / 过一会嘛」这类话，不出图
        - 场景（地点/天气/时刻）、衣服、额度、自拍记忆全部走自拍那套，和特写共用
        - 换装、cos 只有能换衣的地点（家里/住宿）才是随时可用，其它地点先婉拒再按既定衣服拍
        - 管理员（admin）保留原逻辑：任意时间可换装/cos，还能「admin摸摸2」点名第几条
        - 先因地制宜答应一句（「好呀我这就让你摸摸🤳」），再出图
        """
        if not bool(self._cfg("interact_enabled", True)):
            return False
        admin_mode = bool(is_admin and ADMIN_MODE_PAT.search(text))
        if not (admin_mode or self._can_trigger(event, text)):
            return False
        m = self._interaction_match(text)
        if not m and admin_mode:
            m = INTERACT_ADMIN_PAT.search(text)
        if not m:
            return False
        action = m.group(1)
        slot = self._time_slot(int(self.clock.strftime("%H")))

        forced_id = None
        if admin_mode:
            text = ADMIN_MODE_PAT.sub("", text, count=1).strip(" ，,。;；")
            aliases = "|".join(re.escape(x) for x in (self._profile().name, *self._profile().aliases) if x)
            words = "|".join(re.escape(word) for word in sorted(
                self._interact_triggers(), key=len, reverse=True))
            mid = re.search(
                r"(?:" + words + r")\s*(?:一下)?\s*(?:" + aliases + r")?\s*[\-:：]?\s*(\d{1,3})",
                text,
            )
            if mid:
                forced_id = int(mid.group(1))
            logger.info(
                f"[selfie] 管理员模式（互动）：动作={action}，时段={slot}，"
                f"指定编号={forced_id}，剩余文本={text[:40]}"
            )

        # 句子里同时点了「特写 / 自拍」、而库里又没有这个互动词时，让后面的流程接手，
        # 不要因为一个互动词把明确点单的特写/自拍抢掉（有对应互动提示词时仍以互动为先）。
        if not forced_id and not self._interact_has_prompt(action) and (
                CLOSEUP_PAT.search(text) or self._selfie_match(text)):
            return False

        if self._generating:
            await event.send(MessageChain([Plain("等我拍完这张嘛～")]))
            return True
        if not is_admin and self._quota_left(sender) <= 0:
            await event.send(MessageChain([
                Plain(f"今天已经拍够{self._daily_limit()}张啦，明天再给你看～")
            ]))
            return True

        # 触发词在互动库里没有以它开头的提示词：害羞地回一句，不进生图流程。
        # 管理员用「admin摸摸2」点名编号时仍按编号取，方便测试不同动作词。
        if not forced_id:
            pool = self._interact_prompts()
            if not pool:
                await event.send(MessageChain([Plain(INTERACT_NO_FILE_TEXT)]))
                return True
            if not self._interact_has_prompt(action):
                await event.send(MessageChain([
                    Plain(self.rng.choice(INTERACT_SHY_FALLBACK))
                ]))
                logger.info(
                    f"[selfie] 互动词「{action}」在互动库里没有对应提示词，回害羞短句"
                )
                return True

        style_override = self._detect_style_request(text)
        # 「摘掉帽子 / 脱掉外套」只有主人（管理员）说了才生效，和自拍/特写一致
        removal = self._detect_removal(text) if is_admin else {}
        # 换衣门控要用当前场景：家里/住宿任意时段可换，其它地点不行（旧实例保持原规则）
        scene = await self._get_scene()

        # 1) 互动 + cos：只有能换衣的地点（家里/住宿）才进 cosplay 流程
        if COS_KEYWORD_PAT.search(text):
            if not bool(self._cfg("cos_enabled", True)):
                await event.send(MessageChain([
                    Plain("cosplay 我先关掉啦，先拍张普通的互动？")
                ]))
            else:
                cos_name = self._detect_cos(text)
                cos_refuse = "" if admin_mode else self._cos_refusal(slot, scene)
                if cos_refuse:
                    await event.send(MessageChain([Plain(cos_refuse)]))
                    logger.info(f"[selfie] 拒绝互动 cos（地点={scene.get('type')}，时段={slot}），改按既定衣服拍")
                elif not cos_name:
                    await event.send(MessageChain([
                        Plain("好呀，你想让我 cos 谁呀？说个名字就行（日文名、英文名也可以）～")
                    ]))
                    return True
                else:
                    cue = self._pick_interact_prompt(action, forced_id)
                    if cue.get("error"):
                        return await self._interact_cue_error(event, cue)
                    self._generating = True
                    self._spawn_task(self._start_cos_request(
                        event, sender, cos_name, "closeup", cue,
                    ))
                    return True

        # 2) 互动 + 换装：能换衣的地点（家里/住宿）任意时段可换，其它地点婉拒后照常拍
        costume = None
        refuse = "" if admin_mode else self._costume_refusal(slot, scene)
        if not COS_KEYWORD_PAT.search(text):
            costume = await self._detect_costume(text, llm=not refuse)
        if costume and refuse:
            await event.send(MessageChain([Plain(refuse)]))
            logger.info(f"[selfie] 拒绝互动换装（地点={scene.get('type')}，时段={slot}）：{costume.get('name')}")
            costume = None

        cue = self._pick_interact_prompt(action, forced_id)
        if cue.get("error"):
            return await self._interact_cue_error(event, cue)

        eff_style = style_override or self._user_style(sender)
        if costume:
            if self._natural_chat_enabled():
                ack = await self._natural_ack(
                    event,
                    action_line=(
                        f"换成「{costume.get('name') or '那套衣服'}」，"
                        f"再让对方{action}你一下、拍一张近距离特写"
                    ),
                    detail=(
                        f"你要说出这套衣服的名字「{costume.get('name') or '那套衣服'}」，"
                        "方便对方一眼纠错"
                    ),
                    fallback_pool=[self._costume_confirm_text(costume) + "～"],
                )
            else:
                ack = self._costume_confirm_text(costume) + "～"
        else:
            ack = await self._interact_ack(event, action)
        await self._send_reply(event, ack)
        self._generating = True
        self._spawn_task(self._do_closeup(
            event, is_admin, sender, eff_style, removal, costume, cue,
            force_costume=admin_mode, kind=action,
        ))
        return True

    # ------------------------------------------------------------------
    # 拟人化拍照对话：接单 / 出图 / 配文都交给大模型按人格和上下文包装
    # （关闭 natural_chat 时退回「短句 + 🤳」和「图片＋固定配文」的旧行为）
    # ------------------------------------------------------------------
    ACK_FALLBACK = {
        STYLE_ANIME: ["马上给你拍张二次元的", "等我摆个二次元姿势", "好呀，这就去拍"],
        STYLE_REALISTIC: ["好呀，我拍张真人版的", "等我理一下头发就拍", "这就拍张写实的给你"],
    }
    ACK_FALLBACK_DEFAULT = [
        "马上拍给你", "等我摆个姿势", "好呀，我这就拍", "等我整理一下头发", "收到，来自拍一张",
    ]

    SCENE_ACK_FALLBACK = [
        "今天去哪儿我说了算哦，就在现在这儿拍～",
        "我这儿挺好的呀，就在这儿拍给你看～",
        "出门行程我自己定啦，就在现在这儿拍一张～",
    ]

    def _natural_chat_enabled(self) -> bool:
        """拟人化拍照对话开关（默认开）：图单独发，她用自己语气回应和补一句。"""
        return bool(self._cfg("natural_chat", True))

    def _shutter_mark_enabled(self) -> bool:
        """接单第一句是否带（咔嚓）（默认开）。"""
        return bool(self._cfg("shutter_mark", True))

    def _astrbot_segmented_settings(self, event=None) -> dict:
        """AstrBot 全局「分段回复」配置（platform_settings.segmented_reply）。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            config = self.context.get_config(umo or None)
            settings = (config.get("platform_settings") or {}).get("segmented_reply") or {}
            return settings if isinstance(settings, dict) else {}
        except Exception:
            return {}

    async def _conversation_snapshot(self, event):
        """(当前会话最近的聊天行, 对话的 persona_id)；拿不到就空，不影响出图。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        manager = getattr(self.context, "conversation_manager", None)
        if not umo or manager is None:
            return [], None
        try:
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return [], None
            conversation = await manager.get_conversation(umo, cid)
        except Exception:
            return [], None
        return (
            dialogue.history_lines(getattr(conversation, "history", "[]")),
            getattr(conversation, "persona_id", None),
        )

    async def _persona_prompt(self, event=None,
                              conversation_persona: str | None = None) -> str:
        """当前人格设定：优先 AstrBot 人格，自定义原创角色再补上角色设定。"""
        persona = ""
        manager = getattr(self.context, "persona_manager", None)
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if manager is not None and umo:
            try:
                platform = str(
                    getattr(event, "get_platform_name", lambda: "")() or umo.split(":")[0]
                )
                _pid, selected, _forced, _web = await manager.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=conversation_persona,
                    platform_name=platform,
                )
                text = str((selected or {}).get("prompt") or "").strip()
                # 系统默认人格是通用助手，拿来当角色口吻只会更机械，按没有处理。
                if text and not text.startswith("You are a helpful"):
                    persona = text
            except Exception:
                persona = ""
        if not persona:
            persona = (
                "你是这个账号里的角色本人，用第一人称说话，"
                "自然、口语、简短，像真人聊天，不油腻。"
            )
        return persona[:1200]

    async def _llm_text(self, system_prompt: str, prompt: str, *,
                        timeout: float = 15, retries: int = 1) -> str:
        """问一次对话大模型；没配、超时、报错都返回空串，由调用方兜底。

        对话模型在高负载时十几秒不回是常事（用户遇到过 TimeoutError 直接掉到
        兜底文案），所以超时/临时报错后用更长的时限再试一次，两次都失败才兜底。
        """
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return ""
        waits = [float(timeout)]
        for _ in range(max(0, int(retries))):
            waits.append(max(25.0, min(40.0, waits[-1] * 2)))
        failure = None
        for index, wait in enumerate(waits):
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(system_prompt=system_prompt, prompt=prompt), wait,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failure = e
                if index + 1 < len(waits):
                    logger.info(
                        f"[selfie] 拟人回复 {wait:g}s 内没拿到结果（{e!r}），加时重试"
                    )
                    continue
                break
            return str(getattr(resp, "completion_text", "") or "")
        logger.warning(f"[selfie] 拟人回复生成失败，用兜底文案: {failure!r}")
        return ""

    async def _natural_ack(self, event, *, action_line: str, detail: str = "",
                           fallback_pool=None, style_override: str | None = None,
                           scene_req: bool = False, costume: dict | None = None,
                           removal: dict | None = None) -> str:
        """拟人化接单：先像聊天一样回应对方，第一句话末尾是（咔嚓）。"""
        pool = fallback_pool or self.ACK_FALLBACK.get(
            style_override, self.ACK_FALLBACK_DEFAULT
        )
        if not bool(self._cfg("ack_llm", True)):
            return dialogue.finalize_reply(
                self.rng.choice(pool), kacha=self._shutter_mark_enabled()
            )
        history, conversation_persona = await self._conversation_snapshot(event)
        try:
            scene = await self._get_scene()
        except Exception:
            scene = None
        hints = []
        if scene_req:
            hints.append(
                "对方想让你换个地方拍，你要婉拒：说清楚今天去哪儿是你自己安排的，"
                "然后就按你现在的地方拍。"
            )
        if costume:
            hints.append(
                f"对方点名叫你换装成「{costume.get('name') or '那套衣服'}」，"
                "你要先答应去翻衣柜换，不要直接说已经拍好。"
            )
        if removal and removal.get("hat_off"):
            hints.append("对方还让你把帽子摘掉，可以顺口答应。")
        if removal and removal.get("coat_off"):
            hints.append("对方还让你把外套脱掉，可以顺口答应。")
        if style_override == STYLE_ANIME:
            hints.append("对方点名要二次元动漫风格，可以顺口提一句。")
        elif style_override == STYLE_REALISTIC:
            hints.append("对方点名要写实真人风格，可以顺口提一句。")
        system, prompt = dialogue.ack_prompts(
            persona=await self._persona_prompt(event, conversation_persona),
            user_text=self._clean_text(event) if event is not None else "",
            scene_line=self._scene_caption_tail(scene) if isinstance(scene, dict) else "",
            action_line=action_line,
            detail="".join([detail] + hints),
            history=history,
            mark=self._shutter_mark_enabled(),
            recent=self._recent_ack_lines(),
        )
        text = dialogue.usable_ack(await self._llm_text(system, prompt),
                                   recent=self._recent_ack_lines())
        if text:
            reply = dialogue.finalize_reply(text, kacha=self._shutter_mark_enabled())
            self._remember_ack(reply)
            return reply
        return dialogue.finalize_reply(
            self.rng.choice(pool), kacha=self._shutter_mark_enabled()
        )

    def _recent_ack_lines(self) -> list:
        """最近几次接单话（只留第一行），用来避免每次都说同一句。"""
        rows = getattr(self, "_ack_history", None)
        return list(rows) if isinstance(rows, list) else []

    def _remember_ack(self, text: str) -> None:
        first = str(text or "").split("\n")[0].strip()
        if not first:
            return
        rows = getattr(self, "_ack_history", None)
        if not isinstance(rows, list):
            rows = []
        rows.append(first[:40])
        self._ack_history = rows[-8:]


    async def _ack_text(self, event: AstrMessageEvent, style_override: str | None,
                        removal: dict | None = None,
                        costume: dict | None = None,
                        scene_req: bool = False) -> str:
        """接单话。拟人模式：正常回应 + 第一句（咔嚓）；关掉则旧短句 + 🤳。"""
        if self._natural_chat_enabled():
            action_line = "拍一张自拍（手机前置摄像头、第一人称视角）"
            fallback_pool = self.ACK_FALLBACK.get(style_override, self.ACK_FALLBACK_DEFAULT)
            if costume:
                action_line = (
                    f"先去找「{costume.get('name') or '那套衣服'}」换上，再拍一张自拍"
                )
                # 换装兜底仍要报出衣服名字，方便对方一眼纠错
                fallback_pool = [self._costume_confirm_text(costume) + "～"]
            elif scene_req:
                fallback_pool = self.SCENE_ACK_FALLBACK
            return await self._natural_ack(
                event, action_line=action_line, style_override=style_override,
                scene_req=scene_req, costume=costume, removal=removal,
                fallback_pool=fallback_pool,
            )
        # ---- 以下是关闭拟人化后的旧行为，保持原样 ----
        fallback_pool = self.ACK_FALLBACK.get(
            style_override, self.ACK_FALLBACK_DEFAULT
        )
        if costume:
            # 换装：固定回「我去找一件xxx，你要的是这个对吧」，
            # 让用户能直接看出模型把你说的衣服认成了哪一套（认错了一眼就能纠正）
            return self._costume_confirm_text(costume) + "🤳"
        if scene_req:
            fallback_pool = self.SCENE_ACK_FALLBACK
        if not bool(self._cfg("ack_llm", True)):
            return self.rng.choice(fallback_pool) + "🤳"
        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return self.rng.choice(fallback_pool) + "🤳"
            if style_override == STYLE_ANIME:
                style_hint = "对方还点名要这张用二次元动漫风格，可以顺口提一句。"
            elif style_override == STYLE_REALISTIC:
                style_hint = "对方还点名要这张用写实真人照片风格，可以顺口提一句。"
            else:
                style_hint = ""
            look_hints = []
            if scene_req:
                look_hints.append(
                    "对方想让你换个地方拍，你要婉拒：说清楚今天去哪儿是你自己安排的，"
                    "然后就按你现在的地方拍。"
                )
            if costume:
                look_hints.append(
                    f"对方点名叫你换装成「{costume.get('name', '那套衣服')}」，"
                    "你必须先答应一句「去找衣服穿 / 翻一下衣柜」，不要直接说已经拍好。"
                )
            if removal and removal.get("hat_off"):
                look_hints.append("对方还让你把帽子摘掉，可以顺口答应一句。")
            if removal and removal.get("coat_off"):
                look_hints.append("对方还让你把外套脱掉，可以顺口答应一句。")
            resp = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt=(
                        "你是这个账号里的角色本人，用第一人称说话，"
                        "自然、口语、简短，不油腻。"
                    ),
                    prompt=(
                        "有人让你拍一张自拍。"
                        f"{style_hint}"
                        f"{''.join(look_hints)}"
                        "用一句 8~20 字的第一人称中文答应下来（比如马上拍、摆姿势、理头发），"
                        "只输出这一句话，不要引号、不要emoji、不要表情符号、不要解释。"
                    ),
                ),
                timeout=10,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip("“”\"'` \n")
            text = re.sub(r"[🤳📸✨😀-🙏\U0001F300-\U0001FAFF]+", "", text).strip()
            text = text.strip("。！!～~ ")
            if text and 2 <= len(text) <= 40:
                return text + "🤳"
        except Exception as e:
            logger.warning(f"[selfie] 确认语生成失败，用内置短句: {e!r}")
        return self.rng.choice(fallback_pool) + "🤳"

    async def _send_reply(self, event, text: str) -> list[str]:
        """按 AstrBot「分段回复」的配置把回复拆成多条发出去。

        AstrBot 官方的分段回复只在它自己的回复管线里生效；插件里的接单话和
        配文是主动发送的，所以这里读同一份设置（enable / split_mode / regex /
        split_words / words_count_threshold / content_cleanup_rule），再用同样的
        间隔逐段发送，效果和正常对话回复一致。句号不会出现在聊天里。
        """
        settings = self._astrbot_segmented_settings(event)
        segments = dialogue.split_like_astrbot(text, settings) or (
            [str(text).strip()] if str(text or "").strip() else []
        )
        interval = dialogue.segmented_interval(settings)
        for index, segment in enumerate(segments):
            if index and interval:
                await asyncio.sleep(self.rng.uniform(interval[0], interval[1]))
            await event.send(MessageChain([Plain(segment)]))
        return segments

    async def _send_ack(self, event: AstrMessageEvent, style_override: str | None,
                        removal: dict | None = None,
                        costume: dict | None = None,
                        scene_req: bool = False) -> None:
        text = await self._ack_text(event, style_override, removal, costume, scene_req)
        await self._send_reply(event, text)

    async def _photo_caption(self, event, scene: dict, look: dict, *,
                             kind: str, cos_name: str = "") -> str:
        """生成这张照片的第一人称描述，只用于存档（相册 / 日记 / 照片记忆），不发聊天。

        图片已经发出去了，这段文字用户看不到，所以只等 12 秒、不重试：
        拿不到就用调用方的固定配文存档，别为了看不见的文字拖住出图任务。
        """
        history, conversation_persona = await self._conversation_snapshot(event)
        doing_line = str((scene or {}).get("activity") or "").strip()
        if len(doing_line) > 24:
            # 事件动作正文是长篇生图提示词，不能当聊天素材喂给模型。
            doing_line = ""
        look_name = str((look or {}).get("name") or "").strip()
        if len(look_name) > 16:
            look_name = ""
        system, prompt = dialogue.caption_prompts(
            persona=await self._persona_prompt(event, conversation_persona),
            kind=kind,
            scene_line=self._scene_caption_tail(scene) if isinstance(scene, dict) else "",
            doing_line=doing_line,
            look_line=look_name,
            cos_name=cos_name,
            user_text=self._clean_text(event) if event is not None else "",
            history=history,
        )
        return dialogue.usable_caption(
            await self._llm_text(system, prompt, timeout=12, retries=0)
        )

    async def _deliver_photo(self, event, img: bytes, *, scene: dict, look: dict,
                             kind: str, fallback_caption: str,
                             cos_name: str = "") -> str:
        """发一张照片，返回要存档的配文。

        拟人模式：图片单独发，发完不再补任何文字——照片里是什么她自己记着就行
        （生成一份第一人称描述，存进自拍记录 / 相册 / 日记，不发送到聊天）。
        模型不可用时用原来的固定配文存档，同样不发出去。
        """
        if not self._natural_chat_enabled():
            await event.send(MessageChain([Image.fromBytes(img), Plain(fallback_caption)]))
            return fallback_caption
        await event.send(MessageChain([Image.fromBytes(img)]))
        try:
            text = await self._photo_caption(
                event, scene, look, kind=kind, cos_name=cos_name,
            )
        except Exception as e:
            logger.warning(f"[selfie] 照片描述生成失败，用兜底配文存档: {e!r}")
            text = ""
        if not text:
            return fallback_caption
        # 描述只进记忆：按分段规则切成短句、用逗号接成一条存档文本，不往聊天里发。
        settings = self._astrbot_segmented_settings(event)
        segments = dialogue.split_like_astrbot(text, settings) or [text]
        return "，".join(segments) if len(segments) > 1 else segments[0]

    @staticmethod
    def _look_caption(look: dict | None) -> str:
        """照片配文里的穿着短句：「防尘工装」→「穿防尘工装」。"""
        text = str((look or {}).get("caption") or "").strip()
        if not text:
            return ""
        if text.startswith(("穿", "是", "戴", "着", "披")):
            return text
        return "穿" + text

    async def _do_selfie(self, event: AstrMessageEvent, is_admin: bool,
                         style_override: str | None = None,
                         removal: dict | None = None,
                         sender: str = "",
                         costume: dict | None = None,
                         force_costume: bool = False) -> None:
        try:
            scene, _event_action = self._event_photo_scene(await self._get_scene())
            scene = self._roll_home_room(scene)
            model = str(self._cfg("model", DEFAULT_SELFIE_MODEL))
            style = self._style(style_override)
            await self._ensure_ref_day(scene)
            look = self._resolve_look(scene, removal, None, costume, force_costume,
                                      style=style)
            gen_refs = self._generation_refs(scene, style, look)
            async def _shoot() -> bytes:
                nonlocal look
                try:
                    return await self._generate(
                        self._selfie_prompt(scene, model, style, look),
                        model, style, look.get("negative_extra"),
                        **gen_refs,
                    )
                except Exception as e:
                    # 夜间睡衣这条被上游反复拒时，换最保守的一套再拍一次，免得对方什么都收不到；
                    # 额度不足 / key 失效重试也没用，直接上抛。
                    night_names = {s["name"] for s in SLEEPWEAR_SETS}
                    if (str(scene.get("type") or "") not in ("bed", "bed_sleepy")
                            or str(look.get("name") or "") not in night_names
                            or str(look.get("name") or "") == SAFE_NIGHT_WEAR_NAME
                            or self._is_quota_error(e) or "HTTP 401" in str(e)):
                        raise
                    logger.warning(f"[selfie] 睡衣自拍被拒，换「{SAFE_NIGHT_WEAR_NAME}」再试一次: {e!r}")
                    self.state["night_sleepwear"] = {
                        "night": self._night_key(self.clock.time()), "name": SAFE_NIGHT_WEAR_NAME
                    }
                    look = self._resolve_look(scene, removal, style=style)
                    return await self._generate(
                        self._selfie_prompt(scene, model, style, look),
                        model, style, look.get("negative_extra"),
                        **gen_refs,
                    )
            img = await _shoot()
            caption = f"给你～（{self._scene_caption_tail(scene)}）"
            look_phrase = self._look_caption(look)
            if look_phrase:
                caption = f"给你～（{self._scene_caption_tail(scene)}，{look_phrase}）"
            caption = await self._deliver_photo(
                event, img, scene=scene, look=look, kind="自拍",
                fallback_caption=caption,
            )
            entry = self._save_selfie(
                img, scene, look,
                getattr(event, "unified_msg_origin", ""),
                sender, caption, source="chat",
            )
            self._schedule_self_note(entry)
            if not is_admin:
                self._bump_quota(sender)
            logger.info(
                f"[selfie] 已生成自拍并发送（admin={is_admin}, sender={sender}, "
                f"scene={scene.get('type')}, outfit={look.get('name')}, "
                f"hat_off={bool(look.get('hat_off'))}, coat_off={bool(look.get('coat_off'))}, "
                f"style={style}, request_style={style_override}, "
                f"quota_used={self._quota_used(sender) if sender else '-'})"
            )
            self.state["last_selfie"] = {
                "ts": self.clock.time(), "scene": scene.get("desc"), "type": scene.get("type")
            }
            self._save_state()
            return True
        except Exception as e:
            logger.error(f"[selfie] 自拍生成失败: {e!r}")
            try:
                await event.send(MessageChain([Plain(self._generation_failure_text(e))]))
            except Exception:
                pass
            return False
        finally:
            self._generating = False

    # ------------------------------------------------------------------
    # QQ 空间自动发带图说说
    # ------------------------------------------------------------------
    async def _qzone_loop(self) -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await self._maybe_qzone_post()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[selfie] 空间任务异常（已跳过）: {e}")
            await asyncio.sleep(600)

    def _qzone_instance(self):
        try:
            star = self.context.get_registered_star(QZONE_PLUGIN)
            if not star or not getattr(star, "activated", False):
                return None
            return getattr(star, "star_cls", None)
        except Exception:
            return None

    async def _diary_text(self, scene: dict) -> str:
        default = self.rng.choice(
            [
                "今天在外面逛累了，买了杯奶茶坐在路边慢慢喝，风很舒服。",
                "今天路过一家小店，买了个草莓奶油可颂，坐在窗边慢慢吃完了，好满足。",
                "和朋友聊了好久，笑到肚子疼，今天心情很好。",
                "今天没什么大事，就是想给自己拍张照记录一下。",
            ]
        )
        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return default
            weather = scene.get("weather") or {}
            prompt = (
                f"你现在在：{scene.get('desc')}。"
                f"{scene.get('city') or ''}{scene.get('district') or ''}天气："
                f"{weather.get('desc', '多云')} {weather.get('temp', 20)}℃。"
                "写一条 QQ 空间说说，记录你今天这件事（吃了什么、去了哪、做了什么），"
                "1-2 句话，不超过 60 字，第一人称，口语可爱，"
                "不要引号、不要话题标签、不要解释。"
            )
            resp = await provider.text_chat(
                system_prompt=(
                    "你是这个账号里的角色本人，用第一人称说话，"
                    "自然、口语、简短，像真人发空间说说，不油腻。"
                ),
                prompt=prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            text = text.strip("“”\"' \n")
            if text:
                return text[:80]
        except Exception as e:
            logger.warning(f"[selfie] 生成空间说说文案失败，用默认文案: {e}")
        return default

    async def _make_qzone_post(self, force: bool = False) -> bool:
        inst = self._qzone_instance()
        if inst is None:
            logger.warning("[selfie] 空间插件不可用，跳过自动发说说")
            return False
        service = getattr(inst, "service", None)
        if service is None:
            logger.warning("[selfie] 空间插件 service 不存在，跳过")
            return False
        if not force and not self._qzone_budget_left():
            logger.info("[selfie] 今天空间说说的配图额度已用完，顺延")
            return False
        scene = self._event_photo_scene(await self._get_scene())[0]
        diary = await self._diary_text(scene)
        model = str(self._cfg("qzone_model", DEFAULT_QZONE_MODEL))
        await self._ensure_ref_day(scene)
        look = self._resolve_look(scene)
        img = await self._generate(
            self._selfie_prompt(scene, model, None, look),
            model, None, look.get("negative_extra"),
            **self._generation_refs(scene, None, look),
        )
        post = None
        # qzone 的 Post.images 声明为 list[str]，直接传 PNG bytes 会过不了
        # pydantic 校验（Input should be a valid string / string_unicode）；
        # 底层 normalize_images 虽然支持 bytes，但先落盘成文件再传路径最稳妥。
        fd, qzone_img_path = tempfile.mkstemp(
            prefix="selfie_qzone_", suffix=".png"
        )
        try:
            with os.fdopen(fd, "wb") as fp:
                fp.write(img)
            post = await service.publish_post(
                text=diary, images=[qzone_img_path]
            )
        finally:
            try:
                os.remove(qzone_img_path)
            except OSError:
                pass
        logger.info(
            f"[selfie] 已自动发布空间说说: {diary[:60]}（tid={getattr(post, 'tid', '?')}）"
        )
        self._save_selfie(img, scene, look, "qzone", "", diary, source="qzone")
        if not force:
            self._bump_qzone_budget()
        days = self.rng.uniform(QZONE_MIN_DAYS, QZONE_MAX_DAYS)
        self.state["next_qzone_at"] = self.clock.time() + days * 86400
        self._save_state()
        return True

    async def _maybe_qzone_post(self) -> None:
        now = self.clock.time()
        try:
            next_at = float(self.state.get("next_qzone_at") or 0)
        except Exception:
            next_at = 0
        if not next_at:
            self.state["next_qzone_at"] = now + self.rng.uniform(
                QZONE_MIN_DAYS, QZONE_MAX_DAYS
            ) * 86400
            self._save_state()
            return
        if now < next_at:
            return
        if not self._qzone_budget_left():
            tomorrow = self.clock.strftime("%Y-%m-%d", self.clock.localtime(now + 86400))
            self.state["next_qzone_at"] = self.clock.mktime(
                self.clock.strptime(tomorrow + " 10:30", "%Y-%m-%d %H:%M")
            ) + self.rng.randint(0, 3600)
            self._save_state()
            return
        ok = False
        try:
            ok = await self._make_qzone_post(force=False)
        except Exception as e:
            logger.error(f"[selfie] 自动空间说说失败: {e!r}")
        if not ok:
            self.state["next_qzone_at"] = now + 3600
            self._save_state()

    async def terminate(self) -> None:
        workspace_api = getattr(self, "_workspace_api", None)
        if workspace_api is not None:
            await workspace_api.close()
        supervisor = getattr(self, "_task_supervisor", None)
        if supervisor is not None:
            await supervisor.close()
        for attr in ("_task", "_backfill_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
        self._save_state()
        logger.info("[selfie] 已停止")
