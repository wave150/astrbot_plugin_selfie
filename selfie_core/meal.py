"""Meal and cooking photo prompts.

吃饭的照片以前只在文字里写「她吃了什么」，画面里看不到那份饭。这里给每顿饭
准备 5 套可复用的机位/动作（每次自拍随机抽一套），菜名来自当天日程的 ``meal``，
并按菜的种类补上主食搭配，让每一顿「看起来像回事」：

- 家常炒菜 / 炖菜 → 配一小碗白米饭和筷子，不能干吃；
- 面 / 粉 / 馄饨 → 面本身是主食，可以干吃，配一碟醋或辣油；
- 饺子 → 配醋碟；粥 → 配小菜；
- 三明治 → 配一杯饮料；
- 汉堡 / 炸鸡 / 披萨 → 套餐，配可乐和薯条。

做饭阶段（11:00 准备午餐）另有 5 套动作，锅、食材、刀工或热气必须入画。
所有提示词只描述动作和画面，不改变服装与画风。
"""
import random
import re

# 每类菜配什么主食、另一只手在做什么、桌面还要有什么。
STAPLE_RULES = {
    "rice": ("一小碗白米饭", "拿着筷子", "菜装在盘子里，旁边是单独一碗米饭"),
    "rice_bowl": ("一碗清汤", "拿着勺子", "饭和菜盛在同一个碗或盘里"),
    "noodle": ("一小碟醋和辣油", "拿着筷子", "面或粉本身就是主食，可以干吃，不用配米饭"),
    "dumpling": ("一小碟醋", "拿着筷子", "饺子码在盘子里，旁边是醋碟"),
    "congee": ("一碟小菜和一只咸鸭蛋", "拿着勺子", "粥盛在碗里，旁边摆着小菜"),
    "sandwich": ("一杯牛奶或咖啡", "拿起三明治", "三明治对半切开，能看到里面的夹馅"),
    "fastfood": ("一杯可乐和一包薯条", "拿起汉堡", "汉堡或炸鸡装在纸盒或托盘里，旁边是薯条和可乐"),
    "soup": ("一碗白米饭", "拿着汤勺", "汤装在汤碗里，旁边是米饭"),
    "snack": ("一杯饮品", "拿起小吃", "小吃装在小碟或纸袋里"),
}

_RICE_WORDS = ("炒", "炖", "烧", "焖", "蒸", "煎", "溜", "爆")
_NOODLE_WORDS = ("面", "粉", "馄饨", "米线", "河粉")
_FASTFOOD_WORDS = ("汉堡", "炸鸡", "披萨", "薯条", "鸡块")
_SANDWICH_WORDS = ("三明治", "吐司", "贝果")
_DUMPLING_WORDS = ("饺", "锅贴")
_CONGEE_WORDS = ("粥",)
_SOUP_WORDS = ("汤",)
_RICE_BOWL_WORDS = ("饭", "盖浇", "咖喱", "照烧", "炒饭", "拌饭")


def classify(meal):
    """把菜名归到主食类别。顺序很重要：先认快餐 / 快餐类，再认面食和汤。"""
    text = str(meal or "").strip()
    if not text:
        return "rice"
    if any(word in text for word in _FASTFOOD_WORDS):
        return "fastfood"
    if any(word in text for word in _SANDWICH_WORDS):
        return "sandwich"
    if any(word in text for word in _DUMPLING_WORDS):
        return "dumpling"
    if any(word in text for word in _CONGEE_WORDS):
        return "congee"
    if any(word in text for word in _NOODLE_WORDS):
        return "noodle"
    if any(word in text for word in _RICE_BOWL_WORDS):
        return "rice_bowl"
    if any(word in text for word in _SOUP_WORDS):
        return "soup"
    return "rice"


def staple_note(meal):
    """返回 (主食, 另一只手在做什么, 桌面细节)，给提示词直接拼。"""
    return STAPLE_RULES[classify(meal)]


# 5 套「正在吃」的机位 / 动作。{dish} 是这顿的菜，{staple}/{hand}/{table} 由搭配决定。
MEAL_SETUPS = (
    (
        "近距离自拍（上半身和餐桌都入画，能看到桌上的饭菜）：",
        "她坐在{place}，一只手伸向镜头（画面外拿着手机），另一只手{hand}，正在吃{dish}；"
        "桌上摆着{dish}，旁边是{staple}，{table}；",
    ),
    (
        "斜上方的俯拍自拍（餐桌和饭菜占满画面下半部分）：",
        "她坐在{place}，镜头从桌面斜上方拍下去；她一手扶着碗，另一只手{hand}；"
        "桌面上是这一顿：{dish}、{staple}，{table}；",
    ),
    (
        "近距离自拍（她把碗端到胸前，碗里的饭看得清楚）：",
        "她把碗端到胸前，一只手伸向镜头（画面外拿着手机），另一只手{hand}；"
        "碗里是{staple}，桌上摆着{dish}，{table}；",
    ),
    (
        "从餐桌对面拍的自拍（她、碗筷和菜都在同一画面里）：",
        "她坐在{place}，嘴里正在嚼{dish}，一只手伸向镜头（画面外拿着手机），"
        "另一只手{hand}；桌上摆着{dish}，{staple}就在旁边，{table}；",
    ),
    (
        "近距离自拍（吃到一半，桌面留着用过的餐具）：",
        "她刚吃了几口{dish}，一只手伸向镜头（画面外拿着手机），另一只手托着碗；"
        "桌上摆着吃到一半的{dish}和{staple}，{table}，餐具上有用过的痕迹；",
    ),
)

# 5 套「正在做饭」的机位 / 动作。
COOK_SETUPS = (
    (
        "近距离自拍（上半身和灶台都入画，锅里能看到正在做的菜）：",
        "她站在灶台前，一只手伸向镜头（画面外拿着手机），另一只手握着锅铲翻炒锅里的{dish}；"
        "锅里有切好的食材和冒起的热气，灶台边摆着备好的配料；",
    ),
    (
        "近距离自拍（她低头在案板前切菜，案板、刀和食材都入画）：",
        "她在案板前切配菜，一只手伸向镜头（画面外拿着手机），另一只手按着食材握刀；"
        "案板上有切好的菜和装好的配料，旁边是等下要下锅的{dish}；",
    ),
    (
        "近距离自拍（锅盖掀开，蒸汽升起来）：",
        "她站在灶台前掀开锅盖看火候，一只手伸向镜头（画面外拿着手机），"
        "另一只手扶着锅盖；锅里是正在炖的{dish}，蒸汽和香味一起升起来；",
    ),
    (
        "近距离自拍（她端着刚做好的菜，身后的厨房操作台清楚可见）：",
        "她刚把{dish}盛进盘子端起来，一只手伸向镜头（画面外拿着手机），"
        "另一只手托着盘底；身后的操作台上摆着锅、铲和用过的备菜盘；",
    ),
    (
        "近距离自拍（她在餐桌旁摆碗筷，厨房和饭菜都在画面里）：",
        "她把刚做好的{dish}端到餐桌上，一只手伸向镜头（画面外拿着手机），"
        "另一只手正在摆碗筷；桌上铺着桌布，饭菜和餐具刚放好；",
    ),
)

EAT_TAIL = (
    "；这一张必须同时拍到她本人和桌上的饭菜：菜要看得清是什么，主食要一起入画，"
    "不能只拍脸或者只拍空桌子"
)


def _dish_of(scene):
    meal = str((scene or {}).get("meal") or "").strip()
    if meal:
        return meal
    activity = str((scene or {}).get("activity") or "").strip()
    if not activity:
        return ""
    match = re.search(r"[：:]\s*(.+)$", activity)
    if match:
        return match.group(1).strip()
    match = re.search(r"做\s*(.+)$", activity)
    if match:
        return match.group(1).strip()
    match = re.search(r"(?:早餐|午餐|晚餐|夜宵|吃饭)\s*(?:吃|做)?\s*(.+)$", activity)
    return match.group(1).strip() if match else ""


def meal_photo(scene, rng=None):
    """这一张如果是吃饭 / 做饭，返回机位、动作和画面要求；否则返回 None。

    ``scene['living_phase']`` 为 ``cook``（做饭）、``lunch``（在家吃）、
    ``lunch_out``（在外吃）时生效，菜名取 ``scene['meal']``。
    """
    scene = scene if isinstance(scene, dict) else {}
    phase = str(scene.get("living_phase") or "")
    if phase not in ("cook", "lunch", "lunch_out"):
        return None
    dish = _dish_of(scene)
    if not dish:
        return None
    picker = rng if rng is not None else random
    if phase == "cook":
        framing, pose = picker.choice(COOK_SETUPS)
        return {
            "kind": "cook",
            "dish": dish,
            "framing": framing,
            "pose": pose.format(dish=dish),
            "tail": ("；画面里要有真实的厨房动作：锅、刀、食材、蒸汽至少出现两样，"
                     "不能只是站在厨房里摆拍；"),
        }
    staple, hand, table = staple_note(dish)
    framing, pose = picker.choice(MEAL_SETUPS)
    place = "自家厨房的餐桌前" if phase == "lunch" else "附近小餐馆的餐桌前"
    body = pose.format(dish=dish, staple=staple, hand=hand, table=table, place=place)
    detail = str(scene.get("meal_desc") or "").strip()
    if detail:
        body += f"桌上这份菜的细节：{detail}；"
    return {
        "kind": "eat",
        "dish": dish,
        "staple": staple,
        "framing": framing,
        "pose": body,
        "tail": EAT_TAIL + "；不要把她画成端着空碗或只拿着筷子不吃。",
        "place": place,
    }


def meal_cue(dish, description="", rng=None, setup=None):
    """生图测试「吃饭」动作使用：按选中菜品拼一段可直接当动作提示词的文字。

    ``setup`` 指定 5 套机位里的第几套（0 起）；不传或越界时随机抽一套。
    """
    dish = str(dish or "").strip()
    if not dish:
        return ""
    pair = staple_note(dish)
    index = setup if type(setup) is int and 0 <= setup < len(MEAL_SETUPS) else None
    picker = rng if rng is not None else random
    framing, pose = MEAL_SETUPS[index] if index is not None else picker.choice(MEAL_SETUPS)
    text = framing + pose.format(dish=dish, staple=pair[0], hand=pair[1],
                                 table=pair[2], place="餐桌前")
    detail = str(description or "").strip()
    if detail:
        text += f"桌上这份菜的细节：{detail}；"
    return "这一张拍的是她坐在餐桌前吃饭：" + text + EAT_TAIL + "；不要只拍脸或空桌子。"


def meal_text(photo):
    """把 :func:`meal_photo` 的结果拼成一段话（自定义角色用）。"""
    if not photo:
        return ""
    if photo["kind"] == "cook":
        return "这一张拍的是她做饭：" + photo["framing"] + photo["pose"] + photo["tail"]
    return (f"这一张拍的是她在{photo['place']}吃{photo['dish']}："
            + photo["framing"] + photo["pose"] + photo["tail"])


def meal_photo_prompt(scene, rng=None):
    """整段文字版（给自定义角色等没有独立构图逻辑的提示词用）。"""
    return meal_text(meal_photo(scene, rng))


def describe_meal(scene):
    """给日志 / 测试用的简短描述。"""
    dish = _dish_of(scene)
    if not dish:
        return ""
    staple, hand, _table = staple_note(dish)
    return f"{dish}（{staple}，{hand}）"
