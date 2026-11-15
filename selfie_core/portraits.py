"""Pure single-character appearance and prompt construction for custom profiles.

No framework, filesystem, network, clock or random access. Legacy presets remain
in their compatibility path; custom profiles never inherit that identity.
"""
from .character import CharacterProfile
from .scene_context import scene_context_text


# ------------------------- 换衣地点规则（纯函数） -------------------------
# 只有「家里」和「在外住宿」（酒店/旅馆/青旅/寄宿）可以随时点单换衣服；
# 其它地点（街上、学校、车站、车里……）不行。管理员 force 保留旧能力。
# 事件服装：只有 living 的 prepare/active 阶段、且场景带 event_id 时才算；
# 事件结束后的 end / return_home 场景即使还留着 outfit 回显，也不再是规定服装。
HOME_TYPES = frozenset({
    "home", "home_evening", "home_morning", "home_day", "house", "apartment", "residence",
})
# 睡眠场景本身不区分家里/旅店：要看 tags/home 标记，带 lodging 的床不算家。
BED_TYPES = frozenset({"bed", "bed_sleepy"})
LODGING_TYPES = frozenset({
    "lodging", "hotel", "hostel", "hostelry", "guesthouse", "guest_house", "inn",
    "motel", "homestay", "bnb", "dorm", "boarding_house",
})
HOME_ROOMS = frozenset({
    "客厅", "厨房", "餐厅", "卧室", "书房", "阳台", "浴室", "卫生间", "玄关", "家里", "家中",
})
HOME_TAGS = frozenset({
    "home", "house", "apartment", "residence", "living_room", "bedroom", "kitchen",
    "dining_room", "indoor_home",
})
LODGING_TAGS = frozenset({
    "lodging", "hotel", "hostel", "hostelry", "guesthouse", "guest_house", "inn",
    "motel", "homestay", "bnb", "dorm", "青旅", "旅馆", "酒店", "民宿", "客栈", "寄宿",
})
EVENT_PHASES = frozenset({"prepare", "active"})


def _scene_tags(scene) -> set:
    """scene["tags"] 允许 list/逗号串；统一成小写字符串集合，脏数据不报错。"""
    if not isinstance(scene, dict):
        return set()
    raw = scene.get("tags")
    if isinstance(raw, str):
        raw = raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")
    if isinstance(raw, (list, tuple, set, frozenset)):
        return {str(item).strip().lower() for item in raw if str(item).strip()}
    return set()


def _scene_kind(scene) -> str:
    if not isinstance(scene, dict):
        return ""
    return str(scene.get("type") or scene.get("kind") or "").strip().lower()


def scene_is_home(scene) -> bool:
    """这一张是不是在家里（客厅/厨房/卧室/餐厅……）。

    住宿的床（type=bed/bed_sleepy 且带 lodging 标记）不算家——那是旅店/青旅，
    虽然也能换衣服，但要走 lodging 分支，不能混成 home。
    """
    if not isinstance(scene, dict):
        return False
    if scene.get("home") is True:
        return True
    # 明确标记「不是家」的场景（living 事件/住宿场景）不要靠 type 猜回来。
    if scene.get("home") is False:
        return False
    if _scene_tags(scene) & LODGING_TAGS:
        return False
    if _scene_kind(scene) in LODGING_TYPES:
        return False
    if str(scene.get("stay") or "").strip().lower() == "lodging":
        return False
    if _scene_tags(scene) & HOME_TAGS:
        return True
    kind = _scene_kind(scene)
    if kind in HOME_TYPES or kind in BED_TYPES:
        return True
    if str(scene.get("stay") or "").strip().lower() == "home":
        return True
    return str(scene.get("room") or "").strip() in HOME_ROOMS


def scene_is_lodging(scene) -> bool:
    """这一张是不是在外住宿（酒店/旅馆/青旅/寄宿）。"""
    if not isinstance(scene, dict):
        return False
    if _scene_tags(scene) & LODGING_TAGS:
        return True
    if _scene_kind(scene) in LODGING_TYPES:
        return True
    if str(scene.get("stay") or "").strip().lower() == "lodging":
        return True
    return False


def scene_is_event(scene) -> bool:
    """这一张是不是「事件还在进行」的场景。

    living/world 的场景一定带 living_phase：只有 prepare / active 算事件进行中，
    end（活动结束）、return_home（回家）、lodging 都不算——那些场景就算还留着
    outfit 回显，也不是本次活动规定的那身衣服。
    老数据/手写场景没有 living_phase 时，退回「带 event_id 就是事件场景」。
    """
    if not isinstance(scene, dict):
        return False
    phase = str(scene.get("living_phase") or "").strip().lower()
    if phase:
        return phase in EVENT_PHASES and bool(str(scene.get("event_id") or "").strip())
    return bool(str(scene.get("event_id") or "").strip())


def scene_outfit(scene):
    """把 living 引擎写在 scene["outfit"] 的服装（字符串或 dict）规范成 {name, desc}。

    支持 dict 的 name/desc/description/text 几种写法；空串、空 dict、None 返回 None。
    """
    if not isinstance(scene, dict):
        return None
    raw = scene.get("outfit")
    if isinstance(raw, str):
        text = raw.strip()
        return {"name": text, "desc": text} if text else None
    if isinstance(raw, dict):
        name = str(raw.get("name") or "").strip()
        desc = str(raw.get("desc") or raw.get("description") or raw.get("text") or "").strip()
        if not name and not desc:
            return None
        return {"name": name or desc, "desc": desc or name}
    return None


def event_outfit(scene):
    """事件阶段的服装；不是事件场景（或事件没写 outfit）返回 None。"""
    return scene_outfit(scene) if scene_is_event(scene) else None


def can_change_clothes(scene, *, force=False, legacy=False) -> bool:
    """能不能点单换衣服（自拍 / 特写 / 互动 / cos 共用同一套判断）。

    - force=True：管理员旧能力，任意时间、任意地点都能换。
    - legacy=True：老的「18:00-22:00 在家」规则，只给没有 workspace 的旧实例用，
      保持旧行为不变。
    - 其余（新 workspace 实例）：优先用场景自带的 can_change_clothes 布尔值
      （living / world 已经按「家里或住宿」算好），没有就用 tags/home/type 推断；
      事件阶段的服装排他，除 force 外不允许用点单衣服打破它。
    """
    if force:
        return True
    if not isinstance(scene, dict):
        scene = {}
    if legacy:
        return str(scene.get("slot") or "") == "home_evening"
    if event_outfit(scene) is not None:
        return False
    flag = scene.get("can_change_clothes")
    if isinstance(flag, bool):
        return flag
    return scene_is_home(scene) or scene_is_lodging(scene)


def resolve_custom_look(profile: CharacterProfile, scene, removal=None, costume=None,
                        force_costume=False, clothing_policy=None,
                        clothing_access=None, stage_outfit=None, stage_exclusive=False):
    """自定义角色的这一身。

    clothing_access 由调用方用 can_change_clothes 算好传进来（None = 旧的
    slot/event 规则，保持兼容）；stage_outfit 是场景声明的这套服装（事件阶段
    outfit 也在其中），stage_exclusive=True 表示它是事件服装、点单衣服不能打破它。
    """
    removal = removal or {}
    if not isinstance(scene, dict):
        scene = {}
    if clothing_access is None:
        allowed = force_costume or scene.get('slot') == 'home_evening' or scene.get('event') == 'menclub'
    else:
        allowed = bool(clothing_access) or bool(force_costume)
    stage = stage_outfit if isinstance(stage_outfit, dict) and stage_outfit else None
    exclusive = bool(stage and stage_exclusive)
    chosen = costume if (costume and force_costume) else (
        (stage if exclusive else None)
        or (costume if (costume and allowed) else None)
        or stage)
    change = chosen is not None
    outfit = (str(chosen.get('desc') or chosen.get('name') or '') if change else
              profile.default_outfit or '保留当前角色身体参考图中的完整服装')
    policy = clothing_policy or {}
    selected = ''
    if not change:
        if scene.get('slot') == 'sleep' or scene.get('type') in ('bed', 'bed_sleepy'):
            selected = str(policy.get('sleepwear') or '不透明的宽松长袖家居睡衣和长裤')
        elif scene.get('type') == 'school':
            selected = str(policy.get('schoolwear') or '')
        elif scene.get('type') in ('outdoor', 'snow'):
            temp = (scene.get('weather') or {}).get('temp')
            if isinstance(temp, (int, float)) and temp <= 10:
                selected = str(policy.get('winterwear') or '保暖外套、完整内层服装、长裤和围巾')
    if selected:
        outfit = selected
    hat = '' if removal.get('hat_off') or change or scene.get('slot') == 'sleep' else profile.default_hat
    notes = []
    if change:
        notes.append('身份、脸部和发色保持当前角色参考图一致，只将整套服装替换为本次服装描述。')
    if removal.get('hat_off'):
        notes.append('这次摘下帽子，保留角色本来的发型与发色。')
    if removal.get('coat_off'):
        notes.append('这次移除最外层外套，保留角色内层完整服装；没有外套时保持原服装。')
    return dict(hat=hat, hair=profile.hair or '保持当前角色参考图中的发型和发色',
                necklace=False, outer='',inner='',bottom='',extra='',full=outfit,
                name=str(chosen.get('name') or '换装') if change else ('场景服装' if selected else '角色默认服装'),
                caption=str(chosen.get('name') or '') if change else '',
                negative_extra=[],edit_note=''.join(notes),
                hat_off=bool(removal.get('hat_off')),coat_off=bool(removal.get('coat_off')),
                no_hat_clause='头部外观保持当前角色设定一致',mood='',restyle=bool(change or selected))


def build_custom_prompt(profile, scene, look, *, style='realistic', kind='selfie',
                        cue='', costume_description='', interaction=False, costume_reference=False):
    style_text = ('写实照片质感、真实摄影光影与自然皮肤纹理' if style == 'realistic'
                  else '二次元动画风格，线条与色彩保持一致')
    identity = profile.description or '外观、脸部、体型与当前角色参考图保持一致'
    parts = [f'当前唯一角色：{profile.name or "参考图中的原创角色"}。',
             f'角色身份要求：{identity}。',
             f'发型要求：{profile.hair or "按当前角色参考图"}。',
             f'画面风格：{style_text}。']
    if kind == 'cosplay' or costume_reference:
        parts.append('参考图1用于当前角色身份，额外角色图只用于服装，不替换当前角色的脸、发色或身份。')
    if kind == 'closeup':
        parts.append('这是画外相机拍摄的近距离特写，具体景别和机位以本次构图要求为准。')
    else:
        parts.append('这是角色自己的近距离自拍，画面中不出现手机、相机或自拍杆。')
    wear = costume_description or look.get('full') or profile.default_outfit or '保留身体参考图的服装'
    parts.append(f'本次服装：{wear}。')
    if look.get('hat'):
        parts.append(f'本次头饰：{look["hat"]}。')
    parts.append(str(look.get('edit_note') or ''))
    if cue:
        parts.append('这次特写的具体要求（务必照做）：' + cue)
    parts.append('场景与氛围：' + str(scene.get('desc') or '自然生活场景') + '。')
    parts.append(scene_context_text(scene))
    parts.append('保持人物与支撑面、家具的接触和透视一致。手部结构清晰自然，不出现文字、水印或边框。')
    parts.append('允许构图要求中明确指定的画外互动手。' if interaction else '画面只出现当前角色一人。')
    return '\n'.join(p for p in parts if p)
