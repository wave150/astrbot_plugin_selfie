"""原创角色自拍插件（单实例单角色）。

默认预设仍兼容旧角色：一个二次元少女；插件不内置任何城市数据
- 自拍：第一人称手机前置摄像头视角（画面里不出现手机），白天在外、晚上在床上穿睡衣
- 天气：按角色所在地实时查询，下雪穿棉袄堆雪人，夏天晴天去泳池边
- 场景锁定：生成一张照片后，1 小时内地点/场景不变
- 每天有自己的活动；每 2~4 天自动发一条带图 QQ 空间说说
- 普通用户每天最多 2 张，管理员不限次数

版本历史与完整更新日志见仓库根目录的 CHANGELOG.md。
"""

import asyncio
import base64
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
    from .selfie_core.portraits import (
        resolve_custom_look, build_custom_prompt,
        can_change_clothes, event_outfit,
    )
    from .selfie_core.storage import JsonStateStore
    from .selfie_core.network import download_public_image, decode_image_base64
    from .selfie_core.runtime import SystemClock, RandomSource
    from .selfie_core.scene_adaptation import adapt_scene_prompt
    from .selfie_core.jobs import TaskSupervisor, PhotoRequest, PhotoUseCase
    from .selfie_core.image_transport import ImageTransportMixin
    from .selfie_core.workspace_host import WorkspaceHostMixin
    from .selfie_core.language import character_text
    from .selfie_core.scene_context import scene_context_text, local_datetime
    from .selfie_core.materials import (FILENAMES, SEED_VERSIONS, MaterialStore, ensure_files, merge_seed, parse_materials)
    from .selfie_core.living import photo_variation
    from .selfie_core.event_materials import action_name, active_event
    from .selfie_core.sleep import sleep_photo
    from .selfie_core.meal import meal_photo, meal_photo_prompt, meal_text
except ImportError:  # direct local test import
    from selfie_core.defaults import *
    from selfie_core.defaults import _COSTUME_EXCLUDE, _COSTUME_RE, _COSTUME_HINT_PAT
    from selfie_core.world import WorldMixin, WorldNotReady
    from selfie_core.character import CharacterProfile
    from selfie_core.portraits import (
        resolve_custom_look, build_custom_prompt,
        can_change_clothes, event_outfit,
    )
    from selfie_core.storage import JsonStateStore
    from selfie_core.network import download_public_image, decode_image_base64
    from selfie_core.runtime import SystemClock, RandomSource
    from selfie_core.scene_adaptation import adapt_scene_prompt
    from selfie_core.jobs import TaskSupervisor, PhotoRequest, PhotoUseCase
    from selfie_core.image_transport import ImageTransportMixin
    from selfie_core.workspace_host import WorkspaceHostMixin
    from selfie_core.language import character_text
    from selfie_core.scene_context import scene_context_text, local_datetime
    from selfie_core.materials import (FILENAMES, SEED_VERSIONS, MaterialStore, ensure_files, merge_seed, parse_materials)
    from selfie_core.living import photo_variation
    from selfie_core.event_materials import action_name, active_event
    from selfie_core.sleep import sleep_photo
    from selfie_core.meal import meal_photo, meal_photo_prompt, meal_text


class SelfiePlugin(WorkspaceHostMixin, WorldMixin, ImageTransportMixin, Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        # A running instance has one identity snapshot; reload to switch identities.
        self._active_profile = self._profile()
        try:
            data_dir = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            data_dir = os.path.join(os.getcwd(), "data", PLUGIN_NAME)
        self.base_data_dir = data_dir
        if self._active_profile.is_custom:
            data_dir = os.path.join(data_dir, "characters", self._active_profile.storage_key())
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
        self.ref_path = os.path.join(self.plugin_dir, "assets", "ref.png")
        # 编辑模型专用：三视图里裁出来的竖版正面图（编辑模型会沿用参考图的画幅）
        self.ref_front_path = os.path.join(self.plugin_dir, "assets", "ref_front.png")
        self.ref_9x16_path = os.path.join(self.plugin_dir, "assets", "ref_front_9x16.png")
        # 真人版三视图（同一角色的写实照片版），二次元版仍用上面那套
        self.ref_real_path = os.path.join(self.plugin_dir, "assets", "ref_real.png")
        self.ref_real_front_path = os.path.join(self.plugin_dir, "assets", "ref_real_front.png")
        self.ref_real_9x16_path = os.path.join(
            self.plugin_dir, "assets", "ref_real_front_9x16.png"
        )
        self.ref_real_face_path = os.path.join(self.plugin_dir, "assets", "ref_real_face.png")
        # 单图模式专用的「身体 + 脸」合并参考图（只贴一张时比三视图正面像）
        self.ref_real_body_path = os.path.join(
            self.plugin_dir, "assets", "ref_real_faceandbody.png"
        )
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

    def _spawn_task(self, coro):
        supervisor = getattr(self, "_task_supervisor", None)
        if supervisor is None:
            supervisor = self._task_supervisor = TaskSupervisor(
                on_error=lambda message: logger.error("[selfie] 后台任务失败（详细内容不写入共享日志）"))
        return supervisor.spawn(coro)

    async def _generate(self, prompt: str, model: str, style: str | None = None,
                        negative_extra=None, extra_images=None) -> bytes:
        use_case = getattr(self, "_photo_use_case", None)
        if use_case is None:
            use_case = self._photo_use_case = PhotoUseCase(self._generate_impl)
        return await use_case.execute(PhotoRequest(prompt, model, style,
                                                  tuple(negative_extra or ()), tuple(extra_images or ())))

    async def _custom_character_text(self, instruction, fallback):
        chat = None
        if bool(self._cfg("ack_llm", True)):
            try:
                provider = self.context.get_using_provider()
                chat = provider.text_chat if provider is not None else None
            except Exception:
                pass
        return await character_text(self._profile(), instruction, chat, fallback)

    def _selfie_match(self, text):
        profile = self._profile()
        if not profile.is_custom:
            return SELFIE_PAT.search(text or "")
        normalized = text or ""
        for name in sorted((x for x in (profile.name, *profile.aliases) if x), key=len, reverse=True):
            normalized = normalized.replace(name, "你")
        return SELFIE_PAT.search(normalized)

    def _interaction_match(self, text):
        profile = self._profile()
        if not profile.is_custom:
            return INTERACT_PAT.search(text)
        names = [re.escape(x) for x in (profile.name, *profile.aliases) if x]
        if not names:
            return None
        return re.search(r"(摸摸|捏捏)\s*(?:一下)?\s*(?:" + "|".join(names) + ")", text or "")

    def _profile(self):
        active = getattr(self, "_active_profile", None)
        if active is not None:
            return active
        return CharacterProfile.from_config(self.conf, {
            "name": "小汉堡", "aliases": ["小汉堡", "小憨包", "憨包"],
            "hair": CHAR_HAIR, "default_outfit": CHAR_DEFAULT, "default_hat": CHAR_HAT,
        })

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

    # ---------------- 节日 / 睡衣 ----------------
    def _holiday_override(self) -> str:
        raw = str(self._cfg("holiday_override", "") or "").strip().lower()
        return HOLIDAY_ALIASES.get(raw, "")

    @staticmethod
    def _holiday_for(date_str: str) -> str:
        """date_str='YYYY-MM-DD' -> halloween / christmas / spring_festival / ''。"""
        try:
            year, month, day = (int(x) for x in str(date_str).split("-"))
        except Exception:
            return ""
        if (month, day) == (10, 31):
            return "halloween"
        if (month, day) == (12, 25):
            return "christmas"
        if SPRING_FESTIVAL_DATES.get(year) == (month, day):
            return "spring_festival"
        return ""

    def _today_holiday(self) -> str:
        override = self._holiday_override()
        if override:
            return override
        return self._holiday_for(self._today())

    @staticmethod
    def _night_key(now: float) -> str:
        """一个「晚上」的标识：22:00~次日 07:00 算同一晚（凌晨算前一天）。"""
        local = local_datetime(now)
        return local_datetime(now - 86400 if local.hour < 7 else now).strftime('%Y-%m-%d')

    def _night_sleepwear(self) -> dict:
        """今晚的睡衣：同一晚固定，换晚重新随机（且不会连续两晚一样）。"""
        key = self._night_key(self.clock.time())
        rec = self.state.get("night_sleepwear")
        if not isinstance(rec, dict):
            rec = {}
        if rec.get("night") == key and rec.get("name"):
            for item in SLEEPWEAR_SETS:
                if item["name"] == rec["name"]:
                    return item
        last = str(rec.get("name") or "")
        pool_ = [
            s for s in SLEEPWEAR_SETS
            if s["name"] != last and not s.get("fallback_only")
        ] or [s for s in SLEEPWEAR_SETS if not s.get("fallback_only")]
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
                      force_costume: bool = False) -> dict:
        """解析这一张自拍穿什么。

        返回 dict：hat/hair/outer/inner/bottom/extra/full/necklace，以及
        name/caption/negative_extra/edit_note/hat_off/coat_off/no_hat_clause。
        month 只在测试里用，平时取当前月份；costume 是点单换装（能不能换由
        _clothing_access 判定）；scene["outfit"] 是 living 引擎写的事件阶段服装，
        字符串或 dict 都行，优先于点单衣服，只有管理员 force 能盖过它。
        """
        scene = scene if isinstance(scene, dict) else {}
        profile = self._profile()
        # scene["outfit"] 平时是 world 写回来的「当前这身」回显（字符串），只有事件场景
        # 才是 living 引擎给的阶段服装；回显不能再被当成「要重画的新服装」。
        stage = event_outfit(scene)
        if profile.is_custom:
            return resolve_custom_look(profile, scene, removal, costume, force_costume,
                                       clothing_policy={key: self._cfg('outfit_' + key, '')
                                                        for key in ('sleepwear', 'schoolwear', 'winterwear')},
                                       clothing_access=self._clothing_access(scene),
                                       stage_outfit=stage,
                                       stage_exclusive=bool(stage))
        removal = removal if isinstance(removal, dict) else {}
        hat_off = bool(removal.get("hat_off"))
        coat_off = bool(removal.get("coat_off"))
        scene_type = str(scene.get("type") or "")
        slot = str(scene.get("slot") or "")
        weather = scene.get("weather")
        if not isinstance(weather, dict):
            weather = self.state.get("weather") if isinstance(self.state.get("weather"), dict) else {}
        try:
            temp = float(weather.get("temp", 20) or 20)
        except Exception:
            temp = 20.0
        month = int(month or self.clock.strftime("%m", self.clock.localtime()))
        holiday = self._today_holiday()

        look = {
            "hat": CHAR_HAT, "hair": CHAR_HAIR, "necklace": True,
            "outer": "", "inner": "", "bottom": "", "extra": "", "full": "",
            "name": "", "caption": "", "negative_extra": [],
            "edit_note": "", "hat_off": hat_off, "coat_off": coat_off,
            "no_hat_clause": "不戴帽子（头上只有完整的金色头发）",
            # 夜间睡衣的氛围补充（光线/湿度/姿态），和衣服描述分开写，避免把「露」写进提示词
            "mood": "",
            # restyle=True 时提示词会额外要求「别照抄参考图的衣服，按这套重画」
            "restyle": False,
        }
        # 22:00（sleep 段）之后只要不是「事件场景」、也不在网吧包厢，就一律穿睡衣：
        # 在家、在外住店（lodging）都算，别在酒店房间里穿着白天的衣服。
        sleep_slot = str(scene.get("slot") or "") == "sleep"
        is_event_scene = bool(scene.get("event") or scene.get("event_id"))
        is_night = scene_type in ("bed", "bed_sleepy") or (
            sleep_slot and not is_event_scene and not self._is_cybercafe_scene(scene)
        )
        # 这一张到底穿哪套：管理员 force > 事件阶段服装 > 能换衣的地点里的点单 > 场景默认。
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
                "negative_extra": ["帽子", "汉堡造型的帽子", "外套", "校服"],
                "restyle": True,
                "no_hat_clause": (
                    "不戴汉堡帽，头上跟着这套服装自己的头饰（没有头饰就只露出金色长发）"
                ),
            })
            look["edit_note"] += (
                "参考图里的汉堡造型帽子必须去掉，头上换成这套服装自己的头饰"
                "（没有头饰就只露出金色长发）；"
                "这一张要把整套衣服换掉：上衣、下装（裙子或裤子）、鞋袜、配饰全部替换，"
                "不要保留参考图里的黄色外套、黑色吊带背心和短热裤；"
            )
        elif is_night:
            sleepwear = self._night_sleepwear()
            look.update({
                "hat": "", "necklace": False, "full": sleepwear["desc"],
                "name": sleepwear["name"], "caption": sleepwear["name"],
                "mood": str(sleepwear.get("mood") or ""),
                "negative_extra": ["帽子", "汉堡造型的帽子", "外套", "校服"],
                "restyle": True,
            })
        elif scene_type == "pool":
            look.update({
                "hat": "", "necklace": False, "full": "保守款连体泳衣",
                "name": "连体泳衣", "caption": "泳衣",
                "negative_extra": ["帽子", "外套", "校服"],
                "restyle": True,
            })
        elif holiday:
            hol = HOLIDAY_OUTFITS[holiday]
            look.update({
                "full": hol["desc"] + "，" + hol["extra"],
                "name": hol["name"], "caption": hol["name"],
                "restyle": True,
            })
        elif scene_type == "school":
            look.update({
                "outer": JK_OUTER, "inner": JK_INNER,
                "bottom": JK_BOTTOM, "extra": JK_EXTRA,
                "name": "JK 校服", "caption": "JK 校服",
                "restyle": True,
            })
        elif str(scene.get("event") or "") == "menclub":
            # 夜店：哥特/地雷/Y2K/视觉系，整晚固定（除非当场点菜换装）
            club = self._club_outfit()
            look.update({
                "hat": "", "necklace": False,
                "full": str(club["desc"]),
                "name": str(club["name"]), "caption": str(club["name"]),
                "negative_extra": ["帽子", "汉堡造型的帽子", "黄色外套", "校服"],
                "restyle": True,
                "no_hat_clause": "不戴汉堡帽，头上按这套造型处理（妆容和发型也按夜店风格）",
                "edit_note": (
                    "参考图里的汉堡造型帽子必须去掉，黄色外套和吊带背心全部换掉；"
                    "这一张按夜店造型重画：妆容浓、眼线重，整体偏暗黑，"
                    "灯光是地下室那种激光和频闪，人脸被彩光扫到；"
                ),
            })
        elif scene_type in ("outdoor", "snow"):
            winter = month in (11, 12, 1, 2, 3) and temp <= WINTER_TEMP
            if winter or scene_type == "snow":
                look.update({
                    "outer": WINTER_OUTER,
                    "inner": "黑色吊带背心（胸前有黄色汉堡图案）",
                    "bottom": "黑色短热裤",
                    "extra": WINTER_SCARF + ("和手套" if scene_type == "snow" else ""),
                    "name": "鸭绒淡黄色棉袄+红色围巾",
                    "caption": "棉袄和红围巾",
                    "restyle": True,
                })
            elif temp >= 20:
                look.update({
                    "outer": "黄色宽松外套",
                    "inner": "黑色吊带背心（胸前有黄色汉堡图案）",
                    "bottom": "黑色短热裤",
                    "name": "黄色外套+吊带背心+短热裤",
                    "caption": "",
                })
            elif temp >= 10:
                look.update({
                    "outer": "黄色宽松外套",
                    "inner": "黑色吊带背心（胸前有黄色汉堡图案）",
                    "bottom": "黑色短热裤",
                    "extra": "薄卫衣",
                    "name": "黄色外套+薄卫衣",
                    "caption": "",
                })
            else:
                look.update({
                    "outer": "淡黄色薄棉服",
                    "inner": "黑色吊带背心",
                    "bottom": "加绒长裤",
                    "extra": WINTER_SCARF,
                    "name": "薄棉服+红围巾",
                    "caption": "棉服和围巾",
                    "restyle": True,
                })
        else:  # 室内 / 下雨室内
            look.update({
                "outer": "黄色宽松外套",
                "inner": "黑色吊带背心（胸前有黄色汉堡图案）",
                "bottom": "黑色短热裤",
                "name": "黄色外套+吊带背心+短热裤",
                "caption": "",
            })

        # 主人要求：摘掉汉堡帽子 / 脱掉外套 —— 这张自拍里不出现对应物件
        if hat_off and look["hat"]:
            look["hat"] = ""
            look["negative_extra"].append("帽子, 汉堡造型的帽子, 帽檐, 发饰")
            look["edit_note"] += (
                "参考图里的汉堡造型帽子必须去掉：画面里不能出现任何帽子，"
                "头上只有完整的金色长卷发；"
            )
        if coat_off and look.get("outer"):
            look["outer"] = ""
            look["negative_extra"].append("外套, 黄色外套, 羽绒服, 棉袄, 校服外套")
            look["edit_note"] += (
                "参考图里的外套必须去掉：画面里不能出现外套，只保留里面的衣服；"
            )
        return look

    def _outfit_name(self, scene: dict) -> str:
        try:
            return str(self._resolve_look(scene).get("name") or "")
        except Exception:
            return ""

    # ---------------- 场景数据：外部 scenes.txt（热加载） ----------------


    # ---------------- 城市 / 交通 / 过夜 ----------------


    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------
    def _character(self, look: dict) -> str:
        """文生图模型没有参考图，人物外观靠这段文字锁定。"""
        profile = self._profile()
        if profile.is_custom:
            return f"{profile.description or '当前参考图中的原创角色'}，{profile.hair}，穿{self._wear_text(look)}"
        parts = ["成年女性", str(look.get("hair") or CHAR_HAIR), "皮肤白"]
        if look.get("hat"):
            parts.append("戴" + str(look["hat"]))
        else:
            parts.append(str(look.get("no_hat_clause") or "不戴帽子，头上只有完整的金色头发"))
        parts.append("穿" + self._wear_text(look))
        if look.get("necklace"):
            parts.append("脖子上戴着细项链")
        return "，".join(parts)

    def _style(self, override: str | None = None) -> str:
        raw = str(override or self._cfg("style", STYLE_REALISTIC) or "").strip().lower()
        return STYLE_ALIASES.get(raw, STYLE_REALISTIC)

    def _detect_style_request(self, text: str) -> str | None:
        """从用户这句话里判断有没有点名画风（二次元 / 写实真人）。"""
        if not text:
            return None
        if REAL_STYLE_PAT.search(text):
            return STYLE_REALISTIC
        if ANIME_STYLE_PAT.search(text):
            return STYLE_ANIME
        return None

    def _detect_removal(self, text: str) -> dict:
        """识别主人这句话里的「摘掉汉堡帽子 / 脱掉外套」。

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
        # 默认汉堡套装不算换装
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
        if not cand or "汉堡" in cand:
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
                    f"{cand}：这种服装的典型款式，上装、下装、鞋袜和配饰都要画全"
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

    # ---------------- 画风偏好（按 QQ 号记，默认真人版） ----------------
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
        return any(name and name in (text or "") for name in (profile.name, *profile.aliases))

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
        """特写提示词 txt 路径：配置 > 数据目录 > 插件 assets > 桌面上那份。

        配置里写的路径如果不存在，不再直接返回它（那样会打开失败、悄悄掉回内置 8 条），
        而是记一条 WARNING 继续往后找，保证「文件被挪走/删掉」时还有别的备份顶上。
        """
        root = self._material_dir()
        raw = str(self._cfg("closeup_prompt_file", "") or "").strip()
        if raw:
            p = raw if os.path.isabs(raw) else os.path.join(root, raw)
            if os.path.exists(p):
                return p
            logger.warning(f"[selfie] closeup_prompt_file 指的 {p} 不存在，继续找数据目录 / assets / 桌面")
        for cand in (
            os.path.join(root, "closeup_prompts.txt"),
            os.path.join(self.plugin_dir, "closeup_prompts.txt"),
            os.path.join(self.plugin_dir, "assets", "closeup_prompts.txt"),
            *CLOSEUP_PROMPT_FILE_CANDIDATES,
        ):
            if cand and os.path.exists(cand):
                return cand
        return ""

    def _closeup_prompts(self) -> list:
        """读出特写提示词列表：每行一条，返回 [{"id", "text"}]。

        id 就是文件里的编号（也就是工作台「素材库」里显示的编号），
        所以在工作台里改/删某一条时，其它条目的编号不会变。
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
                if entries:
                    self._closeup_cache = (path, mtime, entries)
                    logger.info(f"[selfie] 特写提示词已加载 {len(entries)} 条：{path}")
                    return list(entries)
                logger.warning(f"[selfie] 特写提示词文件是空的，改用内置 8 条：{path}")
            except Exception as e:
                logger.warning(f"[selfie] 读取特写提示词失败，改用内置 8 条: {e!r}")
        logger.warning(
            "[selfie] 找不到特写提示词文件（配置 / assets/closeup_prompts.txt 都没有），"
            "临时用内置 8 条兜底 —— 请检查 closeup_prompt_file"
        )
        return [{"id": index, "text": text}
                for index, text in enumerate(CLOSEUP_PROMPTS_FALLBACK, 1)]

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
                             forced_id: int | None = None) -> dict:
        """抽一条特写提示词（不会连着两次抽到同一条）。

        - pm=False（普通「特写」）：只抽普通池，排除 closeup_pm_ids 里那几条
        - pm=True （「特写pm」）  ：只抽 closeup_pm_ids 里那几条（默认第 2、6 条）
        - forced_id（管理员「admin特写N」）：直接取第 N 条；越界返回 {"error": "out_of_range"}
        编号按 txt 行号从 1 开始；配置配歪（比如 txt 没那么多行）会退回全部，不会抽不出来。
        """
        pool = self._closeup_prompts() or [
            {"id": index, "text": text} for index, text in enumerate(CLOSEUP_PROMPTS_FALLBACK, 1)]
        by_id = {int(entry["id"]): str(entry["text"]) for entry in pool}
        if forced_id:
            try:
                want = int(forced_id)
            except Exception:
                want = 0
            if want not in by_id:
                return {"error": "out_of_range", "id": want, "total": len(pool), "text": ""}
            text = by_id[want]
            self.state["closeup_last"] = text
            self.state["closeup_last_id"] = want
            self.state["closeup_last_pm"] = bool(pm)
            self._save_state()
            logger.info(f"[selfie] 管理员指定特写第 {want} 条：{text[:40]}")
            return {"id": want, "total": len(pool), "text": text, "pm": bool(pm),
                    "tier": "forced", "forced": True}
        pm_ids = self._closeup_pm_ids()
        normal = [(i, t) for i, t in by_id.items() if i not in pm_ids]
        special = [(i, t) for i, t in by_id.items() if i in pm_ids]
        cand = (special or list(by_id.items())) if pm else (normal or list(by_id.items()))
        last = str(self.state.get("closeup_last") or "")
        pick = self.rng.choice(cand)
        others = [e for e in cand if e[1] != last]
        if len(cand) > 1 and pick[1] == last and others:
            pick = self.rng.choice(others)
        idx, text = pick
        self.state["closeup_last"] = text
        self.state["closeup_last_id"] = idx
        self.state["closeup_last_pm"] = bool(pm)
        self._save_state()
        logger.info(
            f"[selfie] 特写提示词抽到编号 {idx}（共 {len(pool)} 条，"
            f"{'pm 特殊池' if pm else '普通池'}）：{text[:40]}"
        )
        return {
            "id": idx, "total": len(pool), "text": text, "pm": bool(pm),
            "tier": "special" if idx in pm_ids else "normal",
        }

    async def _pick_closeup_cue(self, event, pm: bool, forced_id) -> dict | None:
        """管理员指定了编号就按编号来；编号越界回一句范围并返回 None。"""
        cue = self._pick_closeup_prompt(pm, forced_id)
        if cue.get("error") == "out_of_range":
            await event.send(MessageChain([Plain(
                f"编号 {cue['id']} 没有哦，现在一共 {cue['total']} 条，换一个编号？"
            )]))
            return None
        return cue

    @staticmethod
    def _clean_closeup_cue(text) -> str:
        """把 txt 里抽到的那条要求里「背景 / 画风 / 画幅比例」这类小句剔掉。

        例：「服装不变，背景环境为傍晚的室内少女卧室，人物慵懒地靠在椅子上」
        ->  「服装不变，人物慵懒地靠在椅子上」
        背景统一由 _get_scene() 的真实场景来写，画风/画幅由配置决定。
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
        # 画风词就地删（保留同小句里的动作/构图），例如「手绘风格」「采用动漫处理厚涂风插画特写方式」
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

    # ---------------- 互动：摸摸 / 捏捏 的提示词 ----------------
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
            for filename in (INTERACT_PROMPT_FILENAME, "小汉堡互动.txt"):
                cand = os.path.join(base, filename)
                if os.path.exists(cand):
                    return cand
        return ""

    def _interact_prompts(self) -> list:
        """读互动提示词（每行一条；行首编号自动去掉，可带「摸摸：」「捏捏：」标签）。

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
            out = []
            with open(path, encoding="utf-8-sig") as f:
                for raw in f:
                    ln = raw.strip()
                    if not ln or ln.startswith("#"):
                        continue
                    ln = re.sub(r"^\s*\d+\s*[：:.、)）]\s*", "", ln)
                    action = ""
                    for lab in INTERACT_ACTIONS:
                        for sep in ("：", ":", "｜", "|", " "):
                            if ln.startswith(lab + sep):
                                action = lab
                                ln = ln[len(lab) + len(sep):].strip()
                                break
                        if action:
                            break
                    if ln:
                        out.append({"action": action, "text": ln})
            if out:
                self._interact_cache = (path, mtime, out)
                logger.info(f"[selfie] 互动提示词已加载 {len(out)} 条：{path}")
                return [dict(x) for x in out]
            logger.warning(f"[selfie] 互动提示词文件是空的：{path}")
        except Exception as e:
            logger.warning(f"[selfie] 读取互动提示词失败: {e!r}")
        return []

    def _pick_interact_prompt(self, action: str,
                              forced_id: int | None = None) -> dict:
        """抽一条互动提示词（不会连着两次抽到同一条）。

        优先抽带本动作标签的那几条；一条都没有就整个文件里抽。
        forced_id（管理员「admin摸摸2」）按文件行号从 1 开始取。
        """
        pool = self._interact_prompts()
        if not pool:
            return {"error": "no_file", "id": 0, "total": 0, "text": ""}
        if forced_id:
            try:
                want = int(forced_id)
            except Exception:
                want = 0
            if not (1 <= want <= len(pool)):
                return {"error": "out_of_range", "id": want, "total": len(pool), "text": ""}
            pick = pool[want - 1]
            idx = want
        else:
            tagged = [(i, e) for i, e in enumerate(pool, 1) if e.get("action") == action]
            cand = tagged or [(i, e) for i, e in enumerate(pool, 1)]
            last = str(self.state.get("interact_last") or "")
            fresh = [c for c in cand if c[1].get("text") != last]
            idx, pick = self.rng.choice(fresh or cand)
        self.state["interact_last"] = pick.get("text") or ""
        self.state["interact_last_action"] = action
        self._save_state()
        logger.info(
            f"[selfie] 互动提示词（{action}）抽到第 {idx}/{len(pool)} 条："
            f"{str(pick.get('text'))[:40]}"
        )
        return {
            "id": idx, "total": len(pool), "text": pick.get("text") or "",
            "tier": action, "kind": action,
        }

    async def _interact_ack(self, action: str) -> str:
        """接单话：因地制宜地答应让她摸摸 / 捏捏，结尾统一加 🤳。"""
        if self._profile().is_custom:
            return await self._custom_character_text(f"用户请求{action}互动拍照，简短确认准备配合。", "好呀，稍等，我来配合这个动作～")
        pool = INTERACT_ACK_FALLBACK.get(action) or ["好呀，我这就来"]
        text = ""
        if bool(self._cfg("ack_llm", True)):
            try:
                provider = self.context.get_using_provider()
                if provider is None:
                    raise RuntimeError("没有可用的对话模型")
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        system_prompt=(
                            "你是小汉堡，一个热爱自拍的二次元少女，爱吃汉堡，"
                            "有点憨萌，说话像十几岁女生，自然、口语、可爱，不油腻。"
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
        """互动提示词抽不到时的回话（文件缺失 / 管理员编号越界）。"""
        if cue.get("error") == "out_of_range":
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

    async def _describe_character_costume(self, path: str, cos_name: str) -> str:
        """用支持视觉的模型描述角色身上的服装（不要发色发型），双图失败时兜底用。"""
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
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=(
                        f"这张图是动漫角色「{cos_name}」。只描述她身上的服装和配饰："
                        "款式、颜色、材质、长短袖、裙子还是裤子、鞋袜、头饰，从领口到脚。"
                        "不要描述发色、发型、脸和背景。用 30~120 字中文回答。"
                    ),
                    image_urls=[path],
                ),
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
                    prompt=(SELFIE_NOTE_PROMPT.format(kind=kind) if not self._profile().is_custom else
                            f"请客观描述当前角色{self._profile().name}这张{kind}照片可见的外观、服装、动作和场景，不推测身份，不编造未见内容。"),
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
            ("本人未出镜", "本人在画面里"),
            ("本人不在画面中", "本人在画面中"),
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
        """后台补一张照片的「画面自述」（同一张只会补一次）。"""
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
            logger.info(f"[selfie] 开始补 {len(todo)} 张旧照片的画面自述（后台）")
            for e in todo:
                await self._fill_visual(e)
            logger.info("[selfie] 旧照片画面自述补完")
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
            line += f"。画面里：{visual}"
        else:
            line += f"，背景是{(e.get('desc') or '')[:40]}"
        cap = str(e.get("caption") or "").strip()
        if cap:
            line += f"（你当时配文：「{cap}」）"
        return line

    def _cosplay_look(self, cos_name: str, costume_desc: str) -> dict:
        profile = self._profile()
        if profile.is_custom:
            return resolve_custom_look(profile, {}, costume={"name": f"cos {cos_name}", "desc": costume_desc}, force_costume=True)
        return {
            "hat": "", "hair": CHAR_HAIR, "necklace": False,
            "outer": "", "inner": "", "bottom": "", "extra": "",
            "full": costume_desc,
            "name": f"cos {cos_name}", "caption": f"cos {cos_name}",
            "negative_extra": ["帽子", "汉堡造型的帽子", "外套", "校服"],
            "restyle": True,
            "no_hat_clause": (
                "不戴汉堡帽，保留原本的金色长卷发和五官，发型和发色都不能变"
                "（只穿角色的服装，可以戴角色自己的发饰/头饰）"
            ),
            "edit_note": (
                "参考图1里的汉堡造型帽子必须去掉，保留金色长卷发和原本五官；"
            ),
        }

    def _cosplay_prompt(self, scene: dict, model: str | None, style: str | None,
                        look: dict, cos_name: str, costume_desc: str,
                        with_char_image: bool = True) -> str:
        profile = self._profile()
        if profile.is_custom:
            return build_custom_prompt(profile, scene, look, style=self._style(style),
                                       kind="cosplay", costume_description=costume_desc)
        use_ref = self._use_reference(model, style)
        style = self._style(style)
        style_block = STYLE_BLOCK[style]
        expression = self.rng.choice(EXPRESSIONS)
        gesture = self.rng.choice(GESTURES)
        no_hat = "不戴汉堡帽，保留金色长卷发和原本的五官，发色和脸绝对不能改变"
        cos_line = (
            f"这是一次 cosplay：她 cosplay 成《{cos_name}》，"
            f"只穿这个角色的服装和配饰：{costume_desc}。"
        )
        if use_ref:
            if style == STYLE_REALISTIC:
                role = (
                    "请以参考图1里同一位成年女性的脸和发型为准（真人照片，不要二次元化），"
                    "参考图2只是要 cos 的角色服装样式"
                )
                ref_self = "角色本人（同一个成年女性的脸和金色长卷发）"
                base_line = (
                    "角色基础形象必须和参考图1完全一致：金色长卷发、同一张真人脸，{no_hat}；"
                )
                same_person = "脸和发型保持参考图1里同一个人（真人五官，不要二次元化）"
            else:
                role = "请以参考图中的同一个二次元角色"
                ref_self = "角色本人（金发成年女性）"
                base_line = (
                    "角色基础形象必须和参考图1完全一致：金色长卷发、棕色大眼睛、同一张脸，{no_hat}；"
                )
                same_person = "脸和发型保持参考图1里同一个角色"
            if with_char_image:
                ref_desc = f"参考图1是{ref_self}，参考图2是要 cos 的角色形象图。"
            else:
                ref_desc = f"参考图是{ref_self}。"
            head = (
                f"{ref_desc}"
                f"{role}，画一张手机前置摄像头自拍的第一人称视角照片，9:16竖屏构图，"
                "近距离自拍特写（只拍到腰部以上，脸和上半身占满画面）："
                f"{style_block}"
                f"{base_line.format(no_hat=no_hat)}"
                f"{cos_line}"
                "镜头就在画面前方，她朝镜头伸出一只手臂，前臂从画面右下角斜着伸向镜头、"
                "手掌在画面外，画面里只有这条手臂；"
                f"另一只手{gesture}；脸在画面上方占三分之一，{expression}，眼睛直视镜头。"
                f"{same_person}，衣服严格按上面这段描述来画。"
            )
        else:
            head = (
                "手机前置摄像头自拍的第一人称视角照片，9:16竖屏构图，"
                "近距离自拍特写（只拍到腰部以上，脸和上半身占满画面）："
                f"{style_block}"
                "镜头就在画面前方，她朝镜头伸出一只手臂，前臂从画面右下角斜着伸向镜头、"
                "手掌在画面外，画面里只有这条手臂；"
                f"另一只手{gesture}；脸在画面上方占三分之一，{expression}，眼睛直视镜头。"
                f"人物：金发成年女性，金色长卷发，棕色大眼睛，{no_hat}；穿{costume_desc}。"
            )
        return (
            head
            + scene_context_text(scene)
            + f"场景：{scene.get('desc', '')}，背景是真实世界的环境，"
            "有清晰的环境细节、真实材质和自然光影，背景轻微虚化。"
            + STYLE_QUALITY[style]
            + self._scene_hard_note(scene)
        )

    async def _generate_cosplay(self, scene: dict, model: str, style: str,
                                look: dict, entry: dict) -> bytes:
        """优先「角色图 + 自己三视图」双图生成；失败退回先描述服装再单图生成。"""
        cos_name = str(entry.get("name") or "")
        char_path = str(entry.get("path") or "")
        prompt = self._cosplay_prompt(
            scene, model, style, look, cos_name, look.get("full") or "", with_char_image=True
        )
        if (cos_name and char_path and os.path.exists(char_path)
                and self._site_max_refs() >= 2):
            try:
                return await self._generate(
                    prompt, model, style, look.get("negative_extra"),
                    extra_images=[char_path],
                )
            except Exception as e:
                logger.warning(f"[selfie] 双图 cosplay 生成失败，改用描述法: {e!r}")
        desc = await self._describe_character_costume(char_path, cos_name) or look.get("full") or ""
        look2 = dict(look)
        look2["full"] = desc
        prompt2 = self._cosplay_prompt(
            scene, model, style, look2, cos_name, desc, with_char_image=False
        )
        return await self._generate(prompt2, model, style, look2.get("negative_extra"))

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
            await event.send(MessageChain([
                Plain(f"我要 cos {cand['name']} 啦，等我换好给你拍张特写～")
            ]))
            self._generating = True
            self._spawn_task(self._do_closeup(
                event, sender in self._admins(), sender, self._user_style(sender),
                None, None, cue, cand,
            ))
            return True
        await event.send(MessageChain([Plain(f"我要 cos {cand['name']} 啦，等我换好🤳")]))
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
            caption = f"给你～我 cos 的 {cos_name}（在{scene.get('activity', '')}）"
            await event.send(MessageChain([Image.fromBytes(img), Plain(caption)]))
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
                await event.send(MessageChain([Plain("这次没换好……等我缓缓再试")]))
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
                                kind: str = "") -> bytes:
        """生成特写：普通特写单图；特写 cos 先试双图，失败退回「先描述服装」再单图。"""
        cue_text = self._clean_closeup_cue((cue or {}).get("text"))
        prompt = self._closeup_prompt(
            scene, model, style, look, cue,
            cos_name=cos_name, with_char_image=bool(char_path), kind=kind,
        )
        # 最后一道：让 LLM 把动作落进当前场景（服装/动作/机位不动，防悬浮和背景畸形）
        prompt = await self._scene_adapt_prompt(prompt, scene, look, cue_text)
        neg = list(look.get("negative_extra") or [])
        if CLOSEUP_TOE_PAT.search(cue_text):
            neg += CLOSEUP_NEG_TOE
        if CLOSEUP_HAND_PAT.search(cue_text):
            neg += CLOSEUP_NEG_HAND
        if (cos_name and char_path and os.path.exists(char_path)
                and self._site_max_refs() >= 2):
            try:
                return await self._generate(
                    prompt, model, style, neg,
                    extra_images=[char_path],
                )
            except Exception as e:
                logger.warning(f"[selfie] 特写 cos 双图生成失败，改用描述法: {e!r}")
                desc = (
                    await self._describe_character_costume(char_path, cos_name)
                    or look.get("full") or ""
                )
                look2 = dict(look)
                look2["full"] = desc
                prompt2 = self._closeup_prompt(
                    scene, model, style, look2, cue,
                    cos_name=cos_name, with_char_image=False, kind=kind,
                )
                prompt2 = await self._scene_adapt_prompt(prompt2, scene, look2, cue_text)
                return await self._generate(prompt2, model, style, neg)
        return await self._generate(prompt, model, style, neg)

    async def _do_closeup(self, event: AstrMessageEvent, is_admin: bool,
                          sender: str = "", style_override: str | None = None,
                          removal: dict | None = None,
                          costume: dict | None = None,
                          cue: dict | None = None,
                          cand: dict | None = None,
                          force_costume: bool = False,
                          kind: str = "") -> None:
        """拍一张特写。cand 有值就是「特写 cosplay」，否则按当时的衣服拍。

        kind 有值（摸摸 / 捏捏）= 互动特写：构图话术换成「画外伸一只手进来」的版本，
        记忆里也标成互动，别的流程（场景/衣服/额度/配额）完全共用。
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
            style = self._style(style_override)
            cos_name = str((cand or {}).get("name") or "")
            char_path = str((cand or {}).get("path") or "")
            if cos_name:
                costume_desc = (
                    await self._describe_character_costume(char_path, cos_name)
                    or f"《{cos_name}》这个角色身上的整套服装和配饰（按参考图2）"
                )
                look = self._cosplay_look(cos_name, costume_desc)
            else:
                look = self._resolve_look(scene, removal, None, costume, force_costume)
            kind_name = str(kind or "").strip()
            # 互动那两条要求「画外有只手入镜」，生成期间临时剔掉会掐掉那只手的负面词
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
            if (not cos_name) and look.get("caption"):
                caption = f"给你～（{tag}，{self._scene_caption_tail(scene)}，{look['caption']}）"
            await event.send(MessageChain([Image.fromBytes(img), Plain(caption)]))
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
                f"cos={cos_name or '-'}, cue=#{int((cue or {}).get('id') or 0)}"
                f"({(cue or {}).get('tier') or 'normal'}), "
                f"style={style}, quota_used={self._quota_used(sender) if sender else '-'}）"
            )
            return True
        except Exception as e:
            logger.error(f"[selfie] 特写生成失败: {e!r}")
            try:
                tip = (BALANCE_FAIL_TEXT if self._is_quota_error(e) else CLOSEUP_FAIL_TEXT)
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
        profile = self._profile()
        if profile.is_custom:
            refs = []
            for value in profile.reference_candidates():
                path = value if os.path.isabs(value) else os.path.join(self.plugin_dir, value)
                if os.path.isfile(path):
                    hashes = self._img_hashes(path)
                    if hashes:
                        refs.append(hashes)
            return refs
        cached = getattr(self, "_ref_hash_cache", None)
        if cached is not None:
            return cached
        refs = []
        for path in (
            self.ref_9x16_path, self.ref_front_path, self.ref_path,
            self.ref_real_9x16_path, self.ref_real_front_path, self.ref_real_path,
        ):
            if path and os.path.exists(path):
                h = self._img_hashes(path)
                if h:
                    refs.append(h)
        self._ref_hash_cache = refs
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
            line += f"。画面里：{clip(visual, 120)}"
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
            "\n注意：照片里的人是你本人；画面细节以「画面里：」描述为准。"
            "没有画面描述时只按场景穿着回答，不编造人物或细节。"
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
                    + "。照片里的人是你本人；回答以画面描述为准。"
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
        （例如「小汉堡」），平台类型名（aiocqhttp）只在事件对象上。顺带存下来，
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
        """每次 LLM 请求前，把「自己发过哪些照片、画面里有什么」塞进本轮上下文。"""
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
                    f"[selfie] 照片记忆已注入（{len(note)} 字，含画面自述），"
                    "之后每次对话都会带上"
                )
            else:
                logger.debug(f"[selfie] 已注入照片记忆（{len(note)} 字）")
        except Exception as e:
            logger.warning(f"[selfie] 注入照片记忆失败: {e!r}")

    def _use_reference(self, model: str | None = None, style: str | None = None) -> bool:
        """这次生成会不会带参考图（OpenAI 兼容接口用 /images/edits 贴参考图）。"""
        return bool(self._pick_ref(style))

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
        try:
            picked, action, label = photo_variation(event, scene.get("phase_index"), self.rng)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[selfie] 事件随机池抽取失败，按主素材拍: {e!r}")
            return scene, ""
        variant = dict(scene)
        if isinstance(picked, dict):
            text = str(picked.get("text") or "").strip()
            name = str(picked.get("name") or "").strip()
            if text:
                variant["desc"] = text
                variant["scene_variant"] = name
                extra = picked.get("scene")
                if isinstance(extra, dict):
                    for key in ("type", "tags", "room"):
                        if extra.get(key):
                            variant[key] = extra[key]
        if not action:
            # 没有动作随机池的事件：用当前阶段的主动作正文，保证事件动作照样顶替自拍姿势。
            phases = event.get("phases") if isinstance(event.get("phases"), list) else []
            index = scene.get("phase_index")
            part = phases[index] if type(index) is int and 0 <= index < len(phases) else {}
            action = str((part or {}).get("action") or event.get("action") or "").strip()
            if action:
                label = action_name(event, part or event)
        if action:
            variant["event_action"] = action
            variant["activity"] = label or str(variant.get("activity") or "")
        return variant, action

    def _photo_action(self, scene, expression, gesture, night_kind=""):
        """唯一决定「谁顶替默认自拍动作」的地方。

        优先级：睡觉（00:30 后）> 吃饭/做饭 > 事件当前动作 > 默认自拍。
        睡觉、吃饭、事件都只发这一套动作，不会再和「伸手自拍」叠在一起；
        自定义角色走同一个判断，只把 ``custom_text`` 追加到它自己的提示词后面。
        """
        scene = scene if isinstance(scene, dict) else {}
        if night_kind:
            return sleep_photo(night_kind, expression)
        meal = meal_photo(scene, self.rng)
        if meal:
            return {
                "source": "meal",
                "intro": SELFIE_SHOT_INTRO,
                "framing": meal["framing"],
                "pose": meal["pose"],
                "face_line": f"脸在画面上方占三分之一，{expression}，眼睛直视镜头。",
                "light_line": "",
                "tail": meal["tail"],
                "custom_text": "这一张的动作与画面要求：" + meal_text(meal),
            }
        event_action = str(scene.get("event_action") or "").strip()
        if event_action:
            return {
                "source": "event",
                "intro": "一张 9:16 竖屏照片，记录她此刻正在做的事：",
                "framing": "竖屏照片构图（记录她此刻正在做的事）：",
                "pose": ("这一张的动作、机位和景别完全按下面这段来，"
                         f"不要再画成伸手自拍：{event_action}；"),
                "face_line": "",
                "light_line": "",
                "tail": "",
                "custom_text": "这一张的动作与机位（务必照做）：" + event_action,
            }
        return {
            "source": "default",
            "intro": SELFIE_SHOT_INTRO,
            "framing": "近距离自拍特写（只拍到腰部以上，脸和上半身占满画面）：",
            "pose": ("镜头就在画面前方，她朝镜头伸出一只手臂，前臂从画面右下角斜着伸向镜头、"
                     "手掌在画面外，画面里只有这条手臂；"
                     f"另一只手{gesture}；"),
            "face_line": f"脸在画面上方占三分之一，{expression}，眼睛直视镜头。",
            "light_line": "",
            "tail": "",
            "custom_text": "",
        }

    def _selfie_prompt(self, scene: dict, model: str | None = None,
                       style: str | None = None,
                       look: dict | None = None) -> str:
        """按「模型类型 + 画风 + 这套衣服」生成提示词。

        - 带参考图：以参考图中的同一角色为准（脸/发型），衣服按这次解析出来的 look
        - 不带参考图：纯文字人设
        - 画风只有两种：纯二次元动漫 / 写实真人照片；用户当场点名的画风优先
        - 主人要求摘帽子/脱外套时，用 edit_note 明确让模型把参考图里的物件去掉
        - 动作只由 ``_photo_action`` 决定：睡觉 / 吃饭 / 事件动作都会顶替默认自拍姿势
        """
        profile = self._profile()
        scene = scene if isinstance(scene, dict) else {}
        # 吃饭 / 做饭这一张：把菜品库里的描述补进场景，供动作和画面要求引用。
        if str(scene.get("living_phase") or "") in ("cook", "lunch", "lunch_out"):
            dish = str(scene.get("meal") or "").strip()
            info = self._meal_library().get(dish) if dish else None
            if info and info.get("description"):
                scene = dict(scene)
                scene["meal_desc"] = str(info["description"])
        if look is None:
            look = self._resolve_look(scene)
        night_kind = self._night_scene_kind(scene)  # "" / "bed" / "bunk" / "cyber"
        sleepy = str(scene.get("type") or "") == "bed_sleepy"
        expression = self.rng.choice(
            SLEEPY_EXPRESSIONS if (sleepy or night_kind == "cyber") else EXPRESSIONS)
        gesture = self.rng.choice(GESTURES)
        action = self._photo_action(scene, expression, gesture, night_kind)
        if profile.is_custom:
            prompt = build_custom_prompt(profile, scene, look, style=self._style(style))
            if action["custom_text"]:
                prompt += "\n" + action["custom_text"]
            return prompt
        use_ref = self._use_reference(model, style)
        style = self._style(style)
        style_block = STYLE_BLOCK[style]
        wear = self._wear_text(look)
        hat = str(look.get("hat") or "")
        hair = str(look.get("hair") or CHAR_HAIR)
        necklace = "，脖子上戴着细项链" if look.get("necklace") else ""
        edit_note = str(look.get("edit_note") or "")
        no_hat = str(look.get("no_hat_clause") or "不戴帽子（头上只有完整的金色头发）")
        restyle_note = ""
        if use_ref and look.get("restyle"):
            restyle_note = "参考图里的衣服不要照抄，人物的衣服必须按下面这段重新画："
        framing = action["framing"]
        pose = action["pose"]
        face_line = action["face_line"]
        light_line = action["light_line"]
        shot_intro = action["intro"]
        # 夜间睡衣的氛围（光线/湿度/姿态/神态）单独补一句：张力靠这个撑，而不是靠穿得少
        mood = str(look.get("mood") or "").strip()
        # 入睡后的姿势和光线已经单独写死，睡衣 mood 里的「靠床头/开小夜灯」会跟它打架，跳过
        if mood and action["source"] != "sleep":
            pose += f"氛围：{mood}；"
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
                f"角色外观必须和参考图完全一致：{hair}、戴{hat}；" if hat
                else f"角色外观必须和参考图完全一致：{hair}、{no_hat}；"
            )
            head = (
                f"{ref_intro}，"
                f"{shot_intro}"
                f"{framing}"
                f"{style_block}"
                f"{head_clause}"
                f"{restyle_note}"
                f"穿{wear}{necklace}；"
                f"{edit_note}"
                f"{pose}{face_line}"
                f"{same_person}，衣服严格按上面这段描述来画。"
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
                        kind: str = "") -> str:
        """特写提示词。

        和自拍共用「人设 / 这一身衣服 / 现在的场景 / 天气和时刻」，区别只在构图：
        把「手机前置摄像头自拍」换成「镜头贴近的特写（画外有人拿相机在拍，画面里没有手机）」，
        再加上这次从 txt 里随机抽到的那条要求（cue）。
        """
        profile = self._profile()
        if profile.is_custom:
            return build_custom_prompt(profile, scene, look or self._resolve_look(scene),
                                       style=self._style(style), kind="closeup",
                                       cue=self._clean_closeup_cue(str((cue or {}).get("text") or "")),
                                       interaction=bool(kind), costume_reference=bool(cos_name and with_char_image))
        use_ref = self._use_reference(model, style)
        style = self._style(style)
        style_block = STYLE_BLOCK[style]
        if look is None:
            look = self._resolve_look(scene)
        cue_text = self._clean_closeup_cue((cue or {}).get("text"))
        if not cue_text:
            cue_text = "镜头贴近拍一张特写，只拍上半身或局部，人物占满画面"
        if cue_text != str((cue or {}).get("text") or "").strip():
            logger.info(f"[selfie] 特写要求已剔除背景/画风/比例：{cue_text[:50]}")
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
        hat = str(look.get("hat") or "")
        hair = str(look.get("hair") or CHAR_HAIR)
        necklace = "，脖子上戴着细项链" if look.get("necklace") else ""
        edit_note = str(look.get("edit_note") or "")
        no_hat = str(look.get("no_hat_clause") or "不戴帽子（头上只有完整的金色头发）")
        restyle_note = ""
        if use_ref and look.get("restyle"):
            restyle_note = "参考图里的衣服不要照抄，人物的衣服必须按下面这段重新画："
        if kind in INTERACT_ACTIONS:
            # 互动特写：这两条提示词的关键就是「画外伸进来一只手」，不能沿用「不出现手臂」那句
            verb = {
                "摸摸": "轻轻摸着她的脸颊和下巴",
                "捏捏": "用两根手指轻轻捏着她的脸颊",
            }[kind]
            shot = (
                f"这是一张「互动特写」照片：对方正在{verb}。镜头贴近人物、构图大胆裁切，"
                "只突出局部或半身，竖幅构图。"
                "画面里必须有一只手从画外伸进来（那是对方的手，正在碰她的脸），"
                "只出现这一只手、五指根数正常、不出现第二个人影；"
                "画面里不出现手机、不出现自拍杆，当成画外有人近距离举着相机在拍她。"
            )
        else:
            shot = (
                "这是一张「特写」照片：镜头贴近人物、构图大胆裁切，只突出局部或半身；"
                "不是自拍——画面里不出现手机、不出现自拍杆、也不出现伸向镜头的手臂，"
                "当成画外有个人拿相机近距离在拍她。竖幅构图。"
            )
        hour = local_datetime(self.clock.time()).hour
        # 跟场景自己的时段走（场景可能是锁定下来的一张），别用当前时钟和场景打架
        slot = str(scene.get("slot") or "") or self._time_slot(hour)
        now_txt = {
            "out": "此刻在外面", "home_evening": "傍晚在家", "sleep": "深夜在家",
        }.get(slot, "此刻待着")
        atmosphere = scene_context_text(scene) + now_txt

        if use_ref and cos_name:
            if style == STYLE_REALISTIC:
                base = "角色基础形象必须和参考图1完全一致：金色长卷发、同一张真人脸"
                intro = (
                    "参考图1是同一位成年女性（真人照片，不要二次元化），"
                    "参考图2只是要 cos 的角色服装样式"
                )
                intro_nochar = "参考图里是同一位成年女性（真人照片），脸和发型以参考图里的为准"
            else:
                base = "角色基础形象必须和参考图1完全一致：金色长卷发、棕色大眼睛、同一张脸"
                intro = "参考图1是同一个二次元角色，参考图2是要 cos 的角色形象图"
                intro_nochar = "参考图里是同一位二次元角色，脸和发型以参考图里的为准"
            if not with_char_image:
                intro = intro_nochar
                base = base.replace("参考图1", "参考图里")
            cos_line = (
                f"这是一次特写 cosplay：她 cosplay 成《{cos_name}》，"
                f"只穿这个角色的服装和配饰：{wear}；"
                "脸、发色和发型仍然是她本人，不能换脸也不能换发色。"
            )
            head = (
                f"{intro}。{shot}{style_block}"
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
                f"角色外观必须和参考图完全一致：{hair}、戴{hat}；" if hat
                else f"角色外观必须和参考图完全一致：{hair}、{no_hat}；"
            )
            head = (
                f"{intro}。{shot}{style_block}"
                f"{head_clause}{restyle_note}穿{wear}{necklace}；{edit_note}"
                f"{same_person}，衣服严格按上面这段描述来画。"
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
            + "背景就画她此刻所在的这个真实环境，要有清晰的环境细节；"
            + "不要换成其它地方，也不要画成纯色背景、棚拍背景或看不出环境的背景。"
            + STYLE_QUALITY[style]
            + self._scene_hard_note(scene)
        )

    # ------------------------------------------------------------------
    # 生图
    # ------------------------------------------------------------------


    def _ref_candidates(self, style: str) -> tuple[str, ...]:
        """按画风给出参考图候选；对应画风那套素材缺失时退回另一套。"""
        anime = (self.ref_9x16_path, self.ref_front_path, self.ref_path)
        real = (self.ref_real_9x16_path, self.ref_real_front_path, self.ref_real_path)
        if style == STYLE_REALISTIC:
            return real + anime
        return anime + real

    def _pick_ref(self, style: str | None = None, single: bool = False) -> str:
        """按画风挑参考图。

        single=True 表示这次只能贴 1 张（备用站限制）：真人版优先用「身体+脸」合并图，
        比原来的三视图正面像得多。
        """
        profile = self._profile()
        if profile.is_custom:
            return profile.pick_reference(self.plugin_dir, single=single)
        st = self._style(style)
        keys = []
        if single:
            keys.append("ref_image_real_single" if st == STYLE_REALISTIC else "ref_image")
        keys.append("ref_image_real" if st == STYLE_REALISTIC else "ref_image")
        if st == STYLE_REALISTIC:
            keys.append("ref_image")   # 老配置只设过 ref_image 时也认
        for key in keys:
            custom = str(self._cfg(key, "") or "").strip()
            if not custom:
                continue
            path = custom if os.path.isabs(custom) else os.path.join(self.plugin_dir, custom)
            if os.path.exists(path):
                return path
            logger.warning(f"[selfie] {key} 不存在: {path}")
        # 单图模式：真人版优先「身体+脸」合并图
        if single and st == STYLE_REALISTIC and self.ref_real_body_path \
                and os.path.exists(self.ref_real_body_path):
            return self.ref_real_body_path
        # 优先 9:16 竖版（输出画幅跟随参考图），其次 2:3 正面，最后整张三视图
        for path in self._ref_candidates(st):
            if path and os.path.exists(path):
                return path
        return ""

    def _face_ref(self, style: str | None = None) -> str:
        """真人风格额外贴一张脸部特写，只用来强化五官一致性。"""
        profile = self._profile()
        if profile.is_custom:
            head = profile.pick_head_reference(self.plugin_dir)
            return head if head != self._pick_ref(style) else ""
        if self._style(style) != STYLE_REALISTIC:
            return ""
        path = self.ref_real_face_path
        return path if path and os.path.exists(path) else ""






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

            # 0) 画风偏好设置：「自拍风格改为二次元 / 以后都用写实」
            pref_style, rest_text = self._detect_style_pref(text)
            if pref_style:
                if not self._can_trigger(event, text):
                    return
                self._set_user_style(sender, pref_style)
                style_name = "二次元" if pref_style == STYLE_ANIME else "真人"
                await event.send(MessageChain([
                    Plain(f"好呀，以后给你拍的照片都用{style_name}风格啦～")
                ]))
                logger.info(f"[selfie] 用户 {sender} 的画风偏好改为 {pref_style}")
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
            # 画风优先级：本句点名 > 该用户的长期偏好 > 配置默认（真人）
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

        await event.send(MessageChain([Plain(
            f"好～衣柜里一共 {total} 套，随机到第 {pick_idx + 1} 套「{name}」，"
            f"再随机配第 {cue.get('id')}/{cue_total} 条特写🤳"
        )]))
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
        # 而且是主人发的那就不用再 @ 或带「小汉堡」
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
                    cue = await self._pick_closeup_cue(event, pm, forced_id)
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
            await event.send(MessageChain([Plain(self._costume_confirm_text(costume) + "～")]))
        else:
            await event.send(MessageChain([Plain(self._closeup_ack(False))]))
        self._generating = True
        self._spawn_task(self._do_closeup(
            event, is_admin, sender, eff_style, removal, costume, cue,
            force_costume=admin_mode,
        ))
        return True

    # ------------------------------------------------------------------
    # 互动模块：摸摸 / 捏捏（提示词在小汉堡互动.txt，随机特写摇不到）
    # ------------------------------------------------------------------
    async def _handle_interact_request(self, event: AstrMessageEvent, text: str,
                                       sender: str, is_admin: bool) -> bool:
        """处理「摸摸小汉堡 / 捏捏小汉堡」。返回 True 表示这条消息已经被她接下。

        规则：
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
            mid = re.search(
                r"(?:摸摸|捏捏)\s*(?:一下)?\s*(?:" + aliases + r")?\s*[\-:：]?\s*(\d{1,3})",
                text,
            )
            if mid:
                forced_id = int(mid.group(1))
            logger.info(
                f"[selfie] 管理员模式（互动）：动作={action}，时段={slot}，"
                f"指定编号={forced_id}，剩余文本={text[:40]}"
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
            await event.send(MessageChain([
                Plain(self._costume_confirm_text(costume) + "～")
            ]))
        else:
            await event.send(MessageChain([Plain(await self._interact_ack(action))]))
        self._generating = True
        self._spawn_task(self._do_closeup(
            event, is_admin, sender, eff_style, removal, costume, cue,
            force_costume=admin_mode, kind=action,
        ))
        return True

    # ------------------------------------------------------------------
    # 接单确认语：由大模型生成，结尾统一加 🤳
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

    async def _ack_text(self, style_override: str | None,
                        removal: dict | None = None,
                        costume: dict | None = None,
                        scene_req: bool = False) -> str:
        if self._profile().is_custom:
            text = await self._custom_character_text("用户请求拍照，请简短确认正在准备拍照，不要说已经拍好。", "好，稍等，我按当前场景拍一张给你看～")
            return text.rstrip("🤳 ") + "🤳"
        fallback_pool = self.ACK_FALLBACK.get(
            style_override, self.ACK_FALLBACK_DEFAULT
        )
        if costume:
            # 换装：固定回「我去找一件xxx，你要的是这个对吧」，
            # 让用户能直接看出模型把你说的衣服认成了哪一套（认错了一眼就能纠正）
            return self._costume_confirm_text(costume)
        if scene_req:
            fallback_pool = self.SCENE_ACK_FALLBACK
        if not bool(self._cfg("ack_llm", True)):
            return self.rng.choice(fallback_pool)
        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return self.rng.choice(fallback_pool)
            if style_override == STYLE_ANIME:
                style_hint = "对方还点名要这张用二次元动漫画风，可以顺口提一句。"
            elif style_override == STYLE_REALISTIC:
                style_hint = "对方还点名要这张用写实真人照片画风，可以顺口提一句。"
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
                look_hints.append("对方还让你把汉堡帽子摘掉，可以顺口答应一句。")
            if removal and removal.get("coat_off"):
                look_hints.append("对方还让你把外套脱掉，可以顺口答应一句。")
            resp = await asyncio.wait_for(
                provider.text_chat(
                    system_prompt=(
                        "你是小汉堡，一个热爱自拍的二次元少女，爱吃汉堡，"
                        "有点憨萌，说话像十几岁女生，自然、口语、可爱，不油腻。"
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
                return text
        except Exception as e:
            logger.warning(f"[selfie] 确认语生成失败，用内置短句: {e!r}")
        return self.rng.choice(fallback_pool)

    async def _send_ack(self, event: AstrMessageEvent, style_override: str | None,
                        removal: dict | None = None,
                        costume: dict | None = None,
                        scene_req: bool = False) -> None:
        text = await self._ack_text(style_override, removal, costume, scene_req)
        await event.send(MessageChain([Plain(text + "🤳")]))

    async def _do_selfie(self, event: AstrMessageEvent, is_admin: bool,
                         style_override: str | None = None,
                         removal: dict | None = None,
                         sender: str = "",
                         costume: dict | None = None,
                         force_costume: bool = False) -> None:
        try:
            scene, _event_action = self._event_photo_scene(await self._get_scene())
            look = self._resolve_look(scene, removal, None, costume, force_costume)
            model = str(self._cfg("model", DEFAULT_SELFIE_MODEL))
            style = self._style(style_override)
            try:
                img = await self._generate(
                    self._selfie_prompt(scene, model, style, look),
                    model, style, look.get("negative_extra"),
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
                look = self._resolve_look(scene, removal)
                img = await self._generate(
                    self._selfie_prompt(scene, model, style, look),
                    model, style, look.get("negative_extra"),
                )
            caption = f"给你～（{self._scene_caption_tail(scene)}）"
            if look.get("caption"):
                caption = f"给你～（{self._scene_caption_tail(scene)}，{look['caption']}）"
            await event.send(MessageChain([Image.fromBytes(img), Plain(caption)]))
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
                if self._is_quota_error(e):
                    tip = BALANCE_FAIL_TEXT
                else:
                    tip = "拍糊了，等我缓缓再拍……"
                await event.send(MessageChain([Plain(tip)]))
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
        if self._profile().is_custom:
            return f"今天在{scene.get('city') or ''}{scene.get('district') or ''}，{scene.get('activity') or '记录日常'}，拍张照片留念。"
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
                    "你是小汉堡，一个热爱自拍的二次元少女，爱吃汉堡，"
                    "有点憨萌，说话像十几岁女生，自然、口语、可爱，不油腻。"
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
        if self._profile().is_custom and not bool(self._cfg("custom_qzone_enabled", False)):
            logger.info("[selfie] 自定义角色未授权公开空间发布，跳过")
            return False
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
        look = self._resolve_look(scene)
        img = await self._generate(
            self._selfie_prompt(scene, model, None, look),
            model, None, look.get("negative_extra"),
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
