"""参考图库：工作台「参考图」页的上传结果、槽位与条件参考图（不依赖 AstrBot）。

存储布局（都在插件数据目录下）::

    reference_images/library.json        # 槽位、条件与图片元数据
    reference_images/<id>.png            # 归一化后的参考图（长边 <= 1536）
    reference_images/<id>_thumb.jpg      # 页面展示用缩略图（长边 <= 480）

槽位固定四个：

- ``main1``：主站全身三视图（主参考图 1）
- ``main2``：主站头部三视图（主参考图 2）
- ``backup1``：副站参考图 1
- ``backup2``：副站参考图 2（副站不是单参考图时才生效）

另外有一份「夜间素颜」设置（``suyan``）：启用后要配一张素颜三视图，
22:00~次日 07:00 在室内睡觉房间拍的照片，脸部参考图换成这张图；
它不占用上面四个槽位，也不参与条件参考图。

条件参考图按「日期 > 气温 > 概率」的优先级覆盖它配过的位置；每个位置各自取
「命中且配了这个位置」的条件里优先级最高的那一条。

一条条件用 ``images: {slot: 图片编号}`` 同时给主站 / 副站挑图（没选的位置不存），
所以同一个触发条件写一条就能同时换主站和副站。旧数据里的 ``image_id`` /
``image_id2`` / ``targets`` 在读取时由 :func:`migrate_condition` 自动换算；
同一个触发条件、位置不冲突的旧条件由 :func:`merge_equivalent_conditions` 并成一条。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

SLOTS = ("main1", "main2", "backup1", "backup2")
SLOT_LABELS = {
    "main1": "主站参考图1（全身三视图）",
    "main2": "主站参考图2（头部三视图）",
    "backup1": "副站参考图1",
    "backup2": "副站参考图2",
}
SLOT_SITE = {"main1": "main", "main2": "main", "backup1": "backup", "backup2": "backup"}
TARGETS = {
    "main": ({"index": 1, "slot": "main1", "label": "主站参考图1"},
             {"index": 2, "slot": "main2", "label": "主站参考图2"}),
    "backup": ({"index": 1, "slot": "backup1", "label": "副站参考图1"},
               {"index": 2, "slot": "backup2", "label": "副站参考图2"}),
}
CONDITION_TYPES = ("date", "temp", "probability")
CONDITION_LABELS = {"date": "指定日期范围", "temp": "当日平均气温", "probability": "每天概率"}
# 数字越小优先级越高：日期 > 气温 > 概率
PRIORITY = {"date": 0, "temp": 1, "probability": 2}
MAX_EDGE = 1536
THUMB_EDGE = 480
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
ALLOWED_FORMATS = ("PNG", "JPEG", "WEBP")
_ID = re.compile(r"^[0-9a-f]{8,32}$")


class RefLibraryError(ValueError):
    """参考图库的输入有问题；消息直接给用户看。"""


def empty_document():
    return {
        "version": 1,
        "main": {"dual": True, "body": "", "head": ""},
        "backup": {"enabled": False, "single": True, "image": "", "image2": ""},
        # 夜间素颜：22:00 到次日 07:00 在室内睡觉房间时，脸部参考图换成这张素颜三视图
        "suyan": {"enabled": False, "image": ""},
        "images": {},
        "conditions": [],
    }


# ---------------- 纯规则：条件匹配与优先级 ----------------


def _int_or_none(value):
    """年份留空 / 空串都表示「不属于具体年」，换算成 None。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _yearly_in_range(today: date, start_key: int, end_key: int) -> bool:
    """每年重复的日期范围（MMDD 比较，允许跨年：12-20 到 01-05）。"""
    key = today.month * 100 + today.day
    if start_key <= end_key:
        return start_key <= key <= end_key
    return key >= start_key or key <= end_key


def _date_rule_matches(condition: dict, today: date) -> bool:
    """日期条件：年份留空＝每年这段日期都生效；只填开始日期＝只生效那一天。"""
    if "start_month" not in condition and "start_year" not in condition:
        # 旧格式：每年同一天（month / day）
        return (today.month == int(condition.get("month") or 0)
                and today.day == int(condition.get("day") or 0))
    start_month = _int_or_none(condition.get("start_month"))
    start_day = _int_or_none(condition.get("start_day"))
    if not (start_month and start_day):
        return False
    start_year = _int_or_none(condition.get("start_year"))
    end_month = _int_or_none(condition.get("end_month"))
    end_day = _int_or_none(condition.get("end_day"))
    end_year = _int_or_none(condition.get("end_year"))
    if not end_month or not end_day:
        # 没填结束日期：只生效开始那一天（年份留空则每年都有）
        if start_year is None:
            return (today.month, today.day) == (start_month, start_day)
        try:
            return today == date(start_year, start_month, start_day)
        except ValueError:
            return False
    if start_year is None and end_year is None:
        return _yearly_in_range(today, start_month * 100 + start_day,
                                end_month * 100 + end_day)
    crosses_year = (start_month, start_day) > (end_month, end_day)
    if start_year is None:
        start_year = end_year - 1 if crosses_year else end_year
    elif end_year is None:
        end_year = start_year + 1 if crosses_year else start_year
    try:
        start = date(start_year, start_month, start_day)
        end = date(end_year, end_month, end_day)
    except ValueError:
        return False
    return start <= today <= end


def condition_matches(condition, *, today: date | None = None,
                      mean=None, rolls=None) -> bool:
    """这条条件今天命中了吗（只看条件本身，不看目标槽位）。"""
    if not isinstance(condition, dict) or not condition.get("enabled", True):
        return False
    kind = condition.get("type")
    if kind == "date":
        if not isinstance(today, date):
            return False
        return _date_rule_matches(condition, today)
    if kind == "temp":
        if mean is None:
            return False
        try:
            value = float(condition.get("value"))
            mean = float(mean)
        except (TypeError, ValueError):
            return False
        return mean < value if condition.get("op") == "lt" else mean > value
    if kind == "probability":
        return bool((rolls or {}).get(str(condition.get("id") or "")))
    return False


def migrate_condition(item):
    """老格式条件（``image_id`` / ``image_id2`` / ``targets``）转成新格式。

    新格式把「这一条条件在哪个位置贴哪张图」直接写成 ``images``：
    ``{"main1": 图A, "main2": 图B, "backup1": 图C}``。一条条件可以同时设置
    主站和副站，不用再为同一个触发条件写两条。
    """
    if not isinstance(item, dict):
        return {}
    images = item.get("images")
    if isinstance(images, dict):
        return dict(item)
    image_id = str(item.get("image_id") or "")
    image_id2 = str(item.get("image_id2") or "")
    mapped = {}
    for raw in item.get("targets") or ():
        slot = _slot_of(raw)
        if not slot:
            continue
        if slot in ("main2", "backup2") and image_id2:
            mapped[slot] = image_id2
        elif image_id:
            mapped[slot] = image_id
    migrated = {key: value for key, value in item.items()
                if key not in ("image_id", "image_id2", "targets")}
    migrated["images"] = mapped
    return migrated


def public_condition(item):
    """给前端 / 出图用的条件：规整 ``images``，旧日期条件折成「每年同一天」的范围。"""
    condition = migrate_condition(item)
    images = condition.get("images")
    images = images if isinstance(images, dict) else {}
    condition["images"] = {slot: str(images.get(slot)) for slot in SLOTS
                           if slot in images and str(images.get(slot) or "")}
    if condition.get("type") == "date" and "start_month" not in condition:
        try:
            month = int(condition.get("month"))
            day = int(condition.get("day"))
        except (TypeError, ValueError):
            pass
        else:
            condition.update(start_year=None, end_year=None,
                             start_month=month, start_day=day,
                             end_month=None, end_day=None)
            condition.pop("month", None)
            condition.pop("day", None)
    return condition


def _condition_images(condition):
    """这条条件在每个位置贴哪张图；老格式按 targets 还原。"""
    if not isinstance(condition, dict):
        return {}
    images = condition.get("images")
    if isinstance(images, dict):
        return {slot: str(images.get(slot)) for slot in SLOTS
                if slot in images and str(images.get(slot) or "")}
    return dict(public_condition(condition).get("images") or {})


def _date_signature(condition):
    if "start_month" in condition:
        return (condition.get("start_year"), condition.get("start_month"),
                condition.get("start_day"), condition.get("end_year"),
                condition.get("end_month"), condition.get("end_day"))
    return ("legacy", condition.get("month"), condition.get("day"))


def _same_trigger(a, b) -> bool:
    """两条条件是不是同一个触发条件（日期 / 气温；概率各有各的摇奖 id，不算）。"""
    kind = str(a.get("type") or "")
    if kind != str(b.get("type") or "") or kind not in ("date", "temp"):
        return False
    if kind == "date":
        return _date_signature(a) == _date_signature(b)
    return (str(a.get("op") or "lt"), float(a.get("value") or 0)) == \
        (str(b.get("op") or "lt"), float(b.get("value") or 0))


def merge_equivalent_conditions(conditions):
    """把同一个触发条件、位置不冲突的旧条件合并成一条。

    旧版条件一次只能设主站或副站，用户常为同一个「低于 10℃」写两条；新模型一条
    就能同时设置，读取旧数据时顺带合并，页面上就不用再手动并成一条。
    """
    merged = []
    for condition in conditions or ():
        if not isinstance(condition, dict):
            continue
        condition = public_condition(condition)
        target = None
        for item in merged:
            if not _same_trigger(item, condition):
                continue
            if bool(item.get("enabled", True)) != bool(condition.get("enabled", True)):
                continue
            first = item.get("images") or {}
            second = condition.get("images") or {}
            shared = set(first) & set(second)
            if any(first[slot] != second[slot] for slot in shared):
                continue  # 同一个位置给了不同的图：不合并，两条都留着
            target = item
            break
        if target is None:
            merged.append(condition)
            continue
        for slot, image in (condition.get("images") or {}).items():
            target["images"].setdefault(slot, image)
        if not target.get("label") and condition.get("label"):
            target["label"] = condition["label"]
    return merged


def resolve_conditions(conditions, *, today: date | None = None, mean=None,
                       rolls=None) -> dict:
    """每个位置取「命中且给它配了图」的条件里优先级最高的一条。

    返回 ``{slot: 条件行}``，行里带该位置要用的 ``image_id`` 和图片路径；
    没有任何条件命中时返回空字典。
    """
    matched = []
    for condition in conditions or ():
        if condition_matches(condition, today=today, mean=mean, rolls=rolls):
            matched.append(condition)
    # 同优先级保持用户写的顺序，结果稳定可预期
    matched.sort(key=lambda item: PRIORITY.get(item.get("type"), 99))
    picked = {}
    for condition in matched:
        images = _condition_images(condition)
        if not images:
            # 纯规则调用（只给 targets、没有图片编号）也按位置参与优先级
            for raw in condition.get("targets") or ():
                slot = _slot_of(raw)
                if slot and slot not in images:
                    images[slot] = ""
        paths = condition.get("paths") if isinstance(condition.get("paths"), dict) else {}
        for slot in SLOTS:
            if slot in picked or slot not in images:
                continue
            picked[slot] = dict(condition, image_id=images[slot],
                                path=str(paths.get(slot) or ""))
    return picked


def describe_condition(condition) -> str:
    """给日志和页面用的一句话说明。"""
    kind = condition.get("type")
    if kind == "date":
        if "start_month" not in condition and "start_year" not in condition:
            return f"{int(condition.get('month') or 0)} 月 {int(condition.get('day') or 0)} 号"
        start_year = _int_or_none(condition.get("start_year"))
        start_month = _int_or_none(condition.get("start_month")) or 0
        start_day = _int_or_none(condition.get("start_day")) or 0
        end_year = _int_or_none(condition.get("end_year"))
        end_month = _int_or_none(condition.get("end_month"))
        end_day = _int_or_none(condition.get("end_day"))

        def one(year, month, day):
            if year is None:
                return f"{month} 月 {day} 号"
            return f"{year:04d}-{month:02d}-{day:02d}"

        start = one(start_year, start_month, start_day)
        if not end_month or not end_day:
            return ("每年 " if start_year is None else "") + start
        if start_year is None and end_year is None:
            return f"每年 {start} 到 {one(None, end_month, end_day)}"
        if end_year is None:
            end_year = start_year
        return f"{one(start_year, start_month, start_day)} 到 {one(end_year, end_month, end_day)}"
    if kind == "temp":
        value = condition.get("value")
        op = "低于" if condition.get("op") == "lt" else "高于"
        return f"当日平均气温{op} {value}℃"
    if kind == "probability":
        return f"每天 {condition.get('percent')}% 概率"
    return str(condition.get("label") or "条件")


# ---------------- 输入归一化 ----------------


def _text(value, limit, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RefLibraryError(f"{field}格式无效")
    value = value.strip()
    if len(value) > limit:
        raise RefLibraryError(f"{field}太长（最多 {limit} 个字）")
    return value


def _number(value, low, high, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise RefLibraryError(f"{field}必须是数字") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise RefLibraryError(f"{field}必须是数字")
    if not low <= number <= high:
        raise RefLibraryError(f"{field}要在 {low:g} 到 {high:g} 之间")
    return round(number, 1)


def _slot_of(target):
    """条件里的目标写法：{"site","index"} 或 "main1" / "backup2" 字符串。"""
    if isinstance(target, str):
        return target if target in SLOTS else ""
    if not isinstance(target, dict):
        return ""
    site = str(target.get("site") or "")
    try:
        index = int(target.get("index"))
    except (TypeError, ValueError):
        return ""
    for row in TARGETS.get(site, ()):
        if row["index"] == index:
            return row["slot"]
    return ""


def normalize_condition(item, image_ids):
    if not isinstance(item, dict):
        raise RefLibraryError("条件参考图格式无效")
    raw_images = item.get("images")
    images = {}
    if isinstance(raw_images, dict):
        # 新格式：一条条件里分别给主站 / 副站挑图（某个位置不选 = 不替换）
        for slot, value in raw_images.items():
            slot = str(slot)
            value = _text(value, 40, "条件参考图")
            if not value:
                continue
            if slot not in SLOTS:
                raise RefLibraryError("条件参考图位置无效，请刷新页面后重试")
            if value not in image_ids:
                raise RefLibraryError("条件参考图引用的图片不存在，请刷新后重试")
            images[slot] = value
        if not images:
            raise RefLibraryError("条件参考图要给主站或副站至少选一张图")
    else:
        # 旧格式兼容：image_id + targets（+ image_id2）
        image_id = _text(item.get("image_id"), 40, "条件参考图")
        if not image_id:
            raise RefLibraryError("条件参考图要先选一张已上传的图片")
        if image_id not in image_ids:
            raise RefLibraryError("条件参考图引用的图片不存在，请刷新后重试")
        targets = []
        for raw in item.get("targets") or ():
            slot = _slot_of(raw)
            if slot and slot not in targets:
                targets.append(slot)
        if not targets:
            raise RefLibraryError("条件参考图要选至少一个要替换的参考图位置")
        # 第二张参考图只贴在 2 号位置（主站参考图2 / 副站参考图2）
        image_id2 = _text(item.get("image_id2"), 40, "第二张参考图")
        if image_id2:
            if image_id2 not in image_ids:
                raise RefLibraryError("第二张参考图引用的图片不存在，请刷新后重试")
            if image_id2 == image_id:
                image_id2 = ""
            elif not any(slot in targets for slot in ("main2", "backup2")):
                raise RefLibraryError("第二张参考图要先勾上「主站参考图2」或「副站参考图2」")
        for slot in targets:
            images[slot] = image_id2 if slot in ("main2", "backup2") and image_id2 else image_id
    kind = str(item.get("type") or "")
    if kind not in CONDITION_TYPES:
        raise RefLibraryError("条件类型无效，请选择日期 / 气温 / 概率")
    condition = {
        "id": _text(item.get("id"), 40, "条件编号") or uuid.uuid4().hex[:12],
        "label": _text(item.get("label"), 40, "条件名称"),
        "images": {slot: images[slot] for slot in SLOTS if slot in images},
        "type": kind,
        "enabled": bool(item.get("enabled", True)),
    }
    if kind == "temp":
        op = str(item.get("op") or "lt")
        if op not in ("lt", "gt"):
            raise RefLibraryError("气温条件要选「低于」或「高于」")
        condition["op"] = op
        condition["value"] = _number(item.get("value"), -60, 60, "气温阈值")
    elif kind == "date":
        condition.update(_normalize_date_rule(item))
    else:
        condition["percent"] = _number(item.get("percent"), 1, 100, "概率")
    return condition


def _normalize_date_rule(item):
    """日期条件归一化：开始日期必填，结束日期留空＝只生效那一天。

    年份留空＝每年这段日期都生效（填了年份就是具体日期范围）。
    """
    if "start_month" not in item and "start_day" not in item and "month" in item:
        # 旧格式：每年同一天（month / day）
        try:
            month = int(item.get("month"))
            day = int(item.get("day"))
        except (TypeError, ValueError):
            raise RefLibraryError("日期条件要填月份和日期") from None
        try:
            datetime(2024, month, day)  # 闰年校验：允许 2 月 29 日
        except ValueError:
            raise RefLibraryError("日期条件的月或日无效") from None
        return {"start_year": None, "start_month": month, "start_day": day,
                "end_year": None, "end_month": None, "end_day": None}

    def month_day(prefix, field):
        try:
            month = int(item.get(prefix + "_month"))
            day = int(item.get(prefix + "_day"))
        except (TypeError, ValueError):
            raise RefLibraryError(field + "要填月份和日期") from None
        try:
            datetime(2024, month, day)
        except ValueError:
            raise RefLibraryError(field + "的月或日无效") from None
        return month, day

    def year(prefix, field):
        raw = item.get(prefix + "_year")
        if raw is None or raw == "":
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise RefLibraryError(field + "的年份无效") from None
        if not 1900 <= value <= 2999:
            raise RefLibraryError(field + "的年份要在 1900 到 2999 之间")
        return value

    start_month, start_day = month_day("start", "开始日期")
    start_year = year("start", "开始日期")
    raw_end_month = item.get("end_month")
    raw_end_day = item.get("end_day")
    end_month = end_day = None
    if raw_end_month not in (None, "") or raw_end_day not in (None, ""):
        end_month, end_day = month_day("end", "结束日期")
    end_year = year("end", "结束日期")
    if (end_month is None) != (end_year is None) and end_year is not None:
        # 结束月份日期留空却填了结束年份：按单天处理，忽略多余年份
        end_year = None
    if end_month is not None:
        if start_year is None and end_year is None:
            pass  # 每年重复的范围；允许跨年（12-20 到 01-05）
        else:
            crosses_year = (start_month, start_day) > (end_month, end_day)
            if start_year is None:
                start_year = end_year - 1 if crosses_year else end_year
            elif end_year is None:
                end_year = start_year + 1 if crosses_year else start_year
            try:
                start = date(start_year, start_month, start_day)
                end = date(end_year, end_month, end_day)
            except ValueError:
                raise RefLibraryError("开始日期或结束日期无效") from None
            if start > end:
                raise RefLibraryError("结束日期不能早于开始日期")
    elif start_year is not None:
        try:
            date(start_year, start_month, start_day)
        except ValueError:
            raise RefLibraryError("开始日期无效") from None
    return {"start_year": start_year, "start_month": start_month, "start_day": start_day,
            "end_year": end_year, "end_month": end_month, "end_day": end_day}


def normalize_slots(payload, image_ids):
    """槽位设置：main.dual / main.body / main.head / backup.* / suyan.*（只接受已上传的图片编号）。

    ``suyan`` 是夜间素颜：启用时必须有一张已上传的素颜三视图；停用时保留已选的图，
    方便之后再打开。
    """
    if not isinstance(payload, dict):
        raise RefLibraryError("参考图设置格式无效")
    main = payload.get("main") if isinstance(payload.get("main"), dict) else {}
    backup = payload.get("backup") if isinstance(payload.get("backup"), dict) else {}
    suyan = payload.get("suyan") if isinstance(payload.get("suyan"), dict) else {}

    def image_id(value, field):
        value = _text(value, 40, field)
        if value and value not in image_ids:
            raise RefLibraryError("所选参考图不存在，请刷新后重试")
        return value

    document = empty_document()
    document["main"] = {"dual": bool(main.get("dual", True)),
                        "body": image_id(main.get("body"), "主站参考图1"),
                        "head": image_id(main.get("head"), "主站参考图2")}
    if not document["main"]["dual"]:
        document["main"]["head"] = ""
    document["backup"] = {"enabled": bool(backup.get("enabled", False)),
                          "single": bool(backup.get("single", True)),
                          "image": image_id(backup.get("image"), "副站参考图1"),
                          "image2": image_id(backup.get("image2"), "副站参考图2")}
    if not document["backup"]["enabled"]:
        document["backup"].update(image="", image2="")
    if document["backup"]["single"]:
        document["backup"]["image2"] = ""
    suyan_image = image_id(suyan.get("image"), "素颜三视图")
    suyan_enabled = bool(suyan.get("enabled", False))
    if suyan_enabled and not suyan_image:
        raise RefLibraryError("启用素颜要先上传一张素颜三视图照片")
    document["suyan"] = {"enabled": suyan_enabled, "image": suyan_image}
    return document["main"], document["backup"], document["suyan"]


def _decode_image(data: bytes):
    """校验并归一化成 RGB PNG；返回 (png_bytes, thumb_bytes, width, height)。"""
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - 环境缺 Pillow 时给出可读提示
        raise RefLibraryError("当前环境缺少图片处理库，无法保存参考图") from exc
    import io
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in ALLOWED_FORMATS:
                raise RefLibraryError("参考图格式无效，请上传 PNG / JPEG / WebP")
            image.load()
            size = image.size
            if size[0] * size[1] > 40_000_000:
                raise RefLibraryError("参考图像素过大，请压缩后再上传")
            if image.mode in ("RGBA", "LA", "P"):
                converted = image.convert("RGBA")
                canvas = Image.new("RGB", converted.size, (255, 255, 255))
                canvas.paste(converted, mask=converted.split()[-1])
                image = canvas
            else:
                image = image.convert("RGB")
            if max(image.size) > MAX_EDGE:
                image = image.copy()
                image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
            full = io.BytesIO()
            image.save(full, format="PNG", optimize=True)
            thumb = image.copy()
            thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.LANCZOS)
            small = io.BytesIO()
            thumb.save(small, format="JPEG", quality=86)
            return full.getvalue(), small.getvalue(), image.size[0], image.size[1]
    except RefLibraryError:
        raise
    except Exception as exc:  # noqa: BLE001 - 解码失败统一成中文提示
        raise RefLibraryError("参考图格式无效，请上传 PNG / JPEG / WebP") from exc


class ReferenceLibrary:
    """``root`` 下的参考图库；构造和读取都不写盘。"""

    def __init__(self, root):
        self.root = Path(root)
        self.document_path = self.root / "library.json"
        self._mutex = threading.RLock()
        self._cache = None

    # ---- 读写 ----

    def _load(self):
        try:
            stat = self.document_path.stat()
        except OSError:
            return empty_document()
        key = (str(self.document_path), stat.st_mtime_ns, stat.st_size)
        cached = self._cache
        if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
            return deepcopy(cached[1])
        try:
            raw = self.document_path.read_text(encoding="utf-8")
        except OSError:
            return empty_document()
        try:
            document = json.loads(raw)
        except ValueError:
            return empty_document()
        if not isinstance(document, dict):
            return empty_document()
        base = empty_document()
        images = document.get("images")
        base["images"] = images if isinstance(images, dict) else {}
        conditions = document.get("conditions")
        base["conditions"] = merge_equivalent_conditions(
            [item for item in conditions if isinstance(item, dict)]) \
            if isinstance(conditions, list) else []
        for name in ("main", "backup", "suyan"):
            value = document.get(name)
            if isinstance(value, dict):
                base[name].update({k: value.get(k, base[name][k]) for k in base[name]})
        self._cache = (key, deepcopy(base))
        return base

    def _save(self, document):
        self.root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        temporary = self.document_path.with_name(self.document_path.name + ".tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, self.document_path)
        finally:
            if temporary.exists():
                temporary.unlink()
        self._cache = None

    def document(self):
        with self._mutex:
            return self._load()

    # ---- 快照给前端 ----

    def snapshot(self):
        document = self.document()
        images = []
        for image_id, meta in document["images"].items():
            if not isinstance(meta, dict):
                continue
            if not self.image_path(image_id):
                continue  # 文件被删了：列表里不显示幽灵条目
            images.append({
                "id": image_id,
                "label": str(meta.get("label") or ""),
                "role": str(meta.get("role") or "extra"),
                "width": int(meta.get("width") or 0),
                "height": int(meta.get("height") or 0),
                "created_at": meta.get("created_at") or 0,
                "source": str(meta.get("source") or ""),
                "costume": str(meta.get("costume") or ""),
                "hat": bool(meta.get("hat")),
                "outer": bool(meta.get("outer")),
                "single_ref": bool(meta.get("single_ref")),
            })
        images.sort(key=lambda row: (-float(row["created_at"] or 0), row["id"]))
        return {
            "images": images,
            "main": dict(document["main"]),
            "backup": dict(document["backup"]),
            "suyan": {"enabled": bool((document.get("suyan") or {}).get("enabled")),
                      "image": str((document.get("suyan") or {}).get("image") or "")},
            "conditions": [public_condition(item) for item in document["conditions"]],
            "slots": [{"slot": slot, "label": SLOT_LABELS[slot]} for slot in SLOTS],
            "targets": {site: [dict(row) for row in rows] for site, rows in TARGETS.items()},
            "types": [{"type": kind, "label": CONDITION_LABELS[kind]}
                      for kind in CONDITION_TYPES],
            "priority": "日期 > 气温 > 概率 > 默认参考图",
        }

    # ---- 图片文件 ----

    def image_path(self, image_id) -> str:
        value = str(image_id or "")
        if not _ID.match(value):
            return ""
        path = self.root / (value + ".png")
        return str(path) if path.is_file() else ""

    def thumb_path(self, image_id) -> str:
        value = str(image_id or "")
        if not _ID.match(value):
            return ""
        path = self.root / (value + "_thumb.jpg")
        return str(path) if path.is_file() else ""

    def read(self, image_id, *, thumb=False):
        path = self.thumb_path(image_id) if thumb else self.image_path(image_id)
        if not path:
            raise RefLibraryError("参考图不存在，请刷新后重试")
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise RefLibraryError("参考图读取失败，请重新上传") from exc

    def find_source(self, source) -> str:
        """按「来源标记」找已经导入过的同一张图（例如同一张图重复上传时复用）。"""
        value = str(source or "")
        if not value:
            return ""
        for image_id, meta in self.document()["images"].items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("source") or "") == value and self.image_path(image_id):
                return image_id
        return ""

    def save_image(self, data, *, label="", role="extra", source=""):
        if not isinstance(data, (bytes, bytearray)):
            raise RefLibraryError("参考图数据无效")
        data = bytes(data)
        if not data:
            raise RefLibraryError("参考图是空的，请重新选择图片")
        if len(data) > MAX_UPLOAD_BYTES:
            raise RefLibraryError("参考图过大，请压缩到 12MB 以内")
        full, thumb, width, height = _decode_image(data)
        with self._mutex:
            document = self._load()
            image_id = uuid.uuid4().hex[:12]
            self.root.mkdir(parents=True, exist_ok=True)
            for path, payload in ((self.root / (image_id + ".png"), full),
                                  (self.root / (image_id + "_thumb.jpg"), thumb)):
                temporary = path.with_name(path.name + ".tmp")
                try:
                    temporary.write_bytes(payload)
                    os.replace(temporary, path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            document["images"][image_id] = {
                "label": _text(label, 40, "参考图名称") or "参考图",
                "role": role if role in ("main", "backup", "extra") else "extra",
                "width": width, "height": height, "created_at": time.time(),
                "source": _text(source, 300, "参考图来源"),
            }
            self._save(document)
            return {"id": image_id, "label": document["images"][image_id]["label"],
                    "role": document["images"][image_id]["role"],
                    "width": width, "height": height}

    def rename_image(self, image_id, label):
        image_id = _text(image_id, 40, "参考图编号")
        with self._mutex:
            document = self._load()
            meta = document["images"].get(image_id)
            if not isinstance(meta, dict):
                raise RefLibraryError("参考图不存在，请刷新后重试")
            meta["label"] = _text(label, 40, "参考图名称") or meta.get("label") or "参考图"
            self._save(document)
            return dict(meta, id=image_id)

    def delete_image(self, image_id):
        image_id = _text(image_id, 40, "参考图编号")
        with self._mutex:
            document = self._load()
            if image_id not in document["images"]:
                raise RefLibraryError("参考图不存在，请刷新后重试")
            removed_conditions = 0
            updated_conditions = 0
            kept = []
            for item in document["conditions"]:
                condition = public_condition(item)
                images = dict(condition.get("images") or {})
                if image_id not in images.values():
                    kept.append(condition)
                    continue
                trimmed = {slot: value for slot, value in images.items()
                           if value != image_id}
                if not trimmed:
                    removed_conditions += 1
                    continue
                condition["images"] = trimmed
                updated_conditions += 1
                kept.append(condition)
            document["images"].pop(image_id, None)
            document["conditions"] = kept
            for key in ("body", "head"):
                if document["main"].get(key) == image_id:
                    document["main"][key] = ""
            for key in ("image", "image2"):
                if document["backup"].get(key) == image_id:
                    document["backup"][key] = ""
            if str((document.get("suyan") or {}).get("image") or "") == image_id:
                document["suyan"] = {"enabled": False, "image": ""}
            self._save(document)
            for path in (self.image_path(image_id), self.thumb_path(image_id)):
                if path:
                    try:
                        Path(path).unlink()
                    except OSError:
                        pass
            return {"deleted": image_id, "conditions_removed": removed_conditions,
                    "conditions_updated": updated_conditions}

    # ---- 槽位与条件 ----

    def save_slots(self, payload):
        with self._mutex:
            document = self._load()
            payload = payload if isinstance(payload, dict) else {}
            if "suyan" not in payload:
                # 老调用方（例如「填入内置参考图」）只发主站 / 副站：素颜设置保持不变
                payload = dict(payload, suyan=dict(document.get("suyan") or {}))
            main, backup, suyan = normalize_slots(payload, set(document["images"]))
            document["main"], document["backup"] = main, backup
            document["suyan"] = suyan
            self._save(document)
            return self.snapshot()

    def save_costume(self, image_id, costume, hat=False, outer=False, single_ref=False):
        """给某张参考图起一个衣服名字，并标记这套衣服能不能摘帽子 / 脱外套。

        名字留空表示「这张图不当衣服用」，生图测试的日常服装里就不会出现它；
        起了名字之后，测试面板会按名字多出「脱帽 / 脱外套」档位。
        ``single_ref=True`` 表示这张图只当单图参考用：保留名字，但不进生图测试的
        参考图衣服下拉，也不参与帽/外套档位。
        """
        image_id = _text(image_id, 40, "参考图编号")
        name = _text(costume, 40, "衣服名称")
        single_ref = bool(single_ref)
        with self._mutex:
            document = self._load()
            meta = document["images"].get(image_id)
            if not isinstance(meta, dict) or not self.image_path(image_id):
                raise RefLibraryError("参考图不存在，请刷新后重试")
            meta["costume"] = name
            meta["single_ref"] = single_ref
            meta["hat"] = bool(name and hat and not single_ref)
            meta["outer"] = bool(name and outer and not single_ref)
            self._save(document)
            return self.snapshot()

    def costume_rows(self):
        """起过名字的衣服参考图（生图测试的日常服装档位、出图时贴哪张图用）。

        标了「只做单图参考」的图不算衣服，不会出现在这里。
        """
        rows = []
        for image_id, meta in self.document()["images"].items():
            if not isinstance(meta, dict):
                continue
            if meta.get("single_ref"):
                continue
            name = str(meta.get("costume") or "").strip()
            path = self.image_path(image_id)
            if not name or not path:
                continue
            rows.append({"image_id": image_id, "name": name, "path": path,
                         "hat": bool(meta.get("hat")), "outer": bool(meta.get("outer"))})
        return rows

    def save_conditions(self, items):
        if not isinstance(items, list):
            raise RefLibraryError("条件参考图格式无效")
        if len(items) > 50:
            raise RefLibraryError("条件参考图最多 50 条")
        with self._mutex:
            document = self._load()
            ids = set(document["images"])
            seen = set()
            cleaned = []
            for item in items:
                condition = normalize_condition(item, ids)
                if condition["id"] in seen:
                    continue
                seen.add(condition["id"])
                cleaned.append(condition)
            document["conditions"] = cleaned
            self._save(document)
            return self.snapshot()

    def slot_paths(self):
        """默认参考图（不含条件）的槽位 → 文件路径；空串表示这个槽位没配。"""
        document = self.document()
        main, backup = document["main"], document["backup"]
        paths = {slot: "" for slot in SLOTS}
        paths["main1"] = self.image_path(main.get("body"))
        if main.get("dual"):
            paths["main2"] = self.image_path(main.get("head"))
        if backup.get("enabled"):
            paths["backup1"] = self.image_path(backup.get("image"))
            if not backup.get("single"):
                paths["backup2"] = self.image_path(backup.get("image2"))
        return paths

    def configured(self) -> bool:
        """页面是否真的配过参考图（配了才接管出图用的参考图）。"""
        return (any(self.slot_paths().values()) or bool(self.document()["conditions"])
                or bool(self.suyan_path()))

    def suyan_path(self):
        """夜间素颜要贴的那张图；没启用或图片文件没了返回空串。"""
        suyan = self.document().get("suyan") or {}
        if not suyan.get("enabled"):
            return ""
        return self.image_path(suyan.get("image"))

    def condition_rows(self):
        """条件 + 每个位置要贴的图片路径，供出图时直接使用。"""
        rows = []
        for condition in self.document()["conditions"]:
            condition = public_condition(condition)
            images = condition.get("images") or {}
            paths = {}
            for slot, image_id in images.items():
                path = self.image_path(image_id)
                if path:
                    paths[slot] = path
            if not paths:
                continue
            rows.append(dict(deepcopy(condition), paths=paths))
        return rows
