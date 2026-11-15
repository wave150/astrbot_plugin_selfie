"""每日日记整理：把实际观察到的活动记录整理成「上午/中午/下午/晚上/今日一句」。

纯函数在这里，LLM 调用由 host 负责；LLM 失败或输出不合格时用确定性的
fallback 保证日记仍然可读，绝不因为整理失败而丢记录。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

SECTIONS = ("上午", "中午", "下午", "晚上", "今日一句")
_EVAL = ("味道还不错。", "还行，能吃饱。", "有点咸，不过挺下饭。", "热乎的，吃着舒服。",
         "一般般，赶时间随便吃的。", "比想象中好吃。")
_CLOSING_GOOD = ("今天最开心的是在{place}待的那一会儿。", "今天最有意思的是{activity}。",
                 "今天印象最深的是{place}的{activity}。")
_CLOSING_BAD = ("今天有点累，跑了不少地方。", "今天没什么特别的，平平淡淡。",
                "今天走得太久了，腿有点酸。")
_MAX_DIARY = 2000


def _hour(record):
    try:
        value = record.get("at")
        return datetime.fromisoformat(str(value)).hour
    except (AttributeError, TypeError, ValueError):
        return None


def section_for(record):
    """Map one observed record to its diary section by local hour."""
    hour = _hour(record)
    if hour is None:
        return "下午"
    if 5 <= hour < 11:
        return "上午"
    if 11 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    return "晚上"


def _place(record):
    return (str(record.get("city") or "") + str(record.get("district") or "")).strip() or "外面"


def _activity(record):
    text = str(record.get("activity") or "").strip()
    return text or "待着"


def _clean_activity(text):
    text = str(text or "").strip()
    text = re.sub(r"^(准备|正在|去|到|在)", "", text)
    return text or "待着"


def _join(records, limit=3):
    names = []
    for record in records:
        name = _clean_activity(_activity(record))
        if name and name not in names:
            names.append(name)
    return "、".join(names[:limit])


def _meal(records):
    for record in records:
        meal = str(record.get("meal") or "").strip()
        if meal:
            return meal
        text = _activity(record)
        match = re.search(r"(?:午餐|吃饭|吃)[：: ]*(.+)$", text)
        if match:
            return match.group(1).strip()
    return ""


def _pick(day, options):
    digest = hashlib.sha1(str(day).encode("utf-8")).digest()
    return options[digest[0] % len(options)]


def fallback_diary(day, records):
    """Deterministic, always-valid diary used when the LLM cannot help."""
    rows = [row for row in records if isinstance(row, dict)]
    groups = {name: [] for name in SECTIONS[:4]}
    for row in rows:
        groups[section_for(row)].append(row)
    blocks = []
    morning, noon, afternoon, evening = (groups["上午"], groups["中午"],
                                         groups["下午"], groups["晚上"])
    blocks.append("上午：" + ("去了" + _place(morning[0]) + "，" + _join(morning) + "。"
                             if morning else "没出门，在家待着。"))
    meal = _meal(noon) or _meal(rows)
    if noon:
        blocks.append("中午：" + ("在" + _place(noon[0]) + "吃了" + meal + "，" + _pick(day, _EVAL)
                                  if meal else "在" + _place(noon[0]) + "附近随便吃了点。"))
    else:
        blocks.append("中午：" + ("吃了" + meal + "，" + _pick(day, _EVAL) if meal else "简单吃了点东西。"))
    blocks.append("下午：" + ("去了" + _place(afternoon[0]) + "，" + _join(afternoon) + "。"
                             if afternoon else "在家附近待了一下午。"))
    if evening:
        lodging = next((row for row in reversed(evening)
                        if str(row.get("room") or "").strip()), None)
        event_row = next((row for row in reversed(evening) if row.get("event_id")), None)
        pieces = []
        if event_row:
            pieces.append("去" + _place(event_row) + "参加活动，" + _activity(event_row) + "。")
        if lodging:
            pieces.append("晚上在" + _place(lodging) + "的" + str(lodging.get("room")).strip()
                          + "落脚住宿。")
        if not pieces:
            last = evening[-1]
            pieces.append("回到" + _place(last) + "，" + _activity(last) + "。")
        blocks.append("晚上：" + "".join(pieces))
    else:
        blocks.append("晚上：在家待着，早点睡了。")
    if rows:
        pick = rows[len(rows) // 2]
        closing = _pick(day, _CLOSING_GOOD).format(place=_place(pick),
                                                   activity=_clean_activity(_activity(pick)))
    else:
        closing = _pick(day, _CLOSING_BAD)
    if hashlib.sha1((str(day) + "bad").encode("utf-8")).digest()[0] % 4 == 0:
        closing = _pick(day, _CLOSING_BAD)
    blocks.append("今日一句：" + closing)
    return "\n".join(blocks)


def record_lines(records):
    """Human-readable record list for the prompt (local time, place, activity)."""
    lines = []
    for row in records:
        if not isinstance(row, dict):
            continue
        try:
            stamp = datetime.fromisoformat(str(row.get("at"))).strftime("%H:%M")
        except (TypeError, ValueError):
            stamp = "--:--"
        line = stamp + " " + _place(row) + "：" + _activity(row)
        meal = str(row.get("meal") or "").strip()
        if meal:
            line += "（吃的是" + meal + "）"
        room = str(row.get("room") or "").strip()
        if room:
            line += "（住宿：" + room + "）"
        if row.get("event_id"):
            line += "（事件）"
        if line not in lines:
            lines.append(line)
    return lines


def build_prompt(day, records):
    """Chinese prompt asking for the fixed five-part diary format."""
    lines = record_lines(records)
    listing = "\n".join(lines) if lines else "（当天没有留下活动记录）"
    return (
        "你是这个角色本人，请把「今天真实发生过的活动记录」整理成一篇第一人称生活日记。\n"
        "硬性要求：\n"
        "1. 只能写记录里出现过的地点、时间和事情，记录没写的不要编造；可以合理承接，但不能新增地点或事件。\n"
        "2. 必须严格按五段输出，每段以固定开头，一段一行：\n"
        "   上午：今天上午去了哪里、做了什么\n"
        "   中午：吃了什么，并给一句真实口吻的评价\n"
        "   下午：去了哪里、做了什么\n"
        "   晚上：在哪里落脚住宿；如果晚上有事件，就写晚上做了什么\n"
        "   今日一句：今天最有意思的一件事，或者吐槽一句今天的烦恼，只写一句\n"
        "3. 全文 200-400 字，口语、第一人称，不要分点符号、不要 markdown、不要解释、不要重复日期。\n"
        "4. 记录里没有上午/中午内容时，就写当天那个时段确实在做的事（例如在家休息），不要虚构出门。\n"
        "日期：" + str(day) + "\n"
        "活动记录：\n" + listing
    )


_MARKERS = ("上午：", "中午：", "下午：", "晚上：", "今日一句：")


def clean_diary(text):
    """Validate/normalize an LLM diary; return '' when it must fall back."""
    if not isinstance(text, str):
        return ""
    value = text.strip()
    if value.startswith("```"):
        value = value.strip("`")
        value = value.split("\n", 1)[-1] if "\n" in value else value
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or len(value) > _MAX_DIARY:
        return ""
    if any(marker not in value for marker in _MARKERS):
        return ""
    if "无法" in value[:40] and "记录" in value[:40]:
        return ""
    # 只保留从「上午：」开始的主体，去掉模型可能加的前言/标题。
    start = value.find("上午：")
    if start > 0:
        value = value[start:].strip()
    lines = [line.strip() for line in value.split("\n") if line.strip()]
    return "\n".join(lines)
