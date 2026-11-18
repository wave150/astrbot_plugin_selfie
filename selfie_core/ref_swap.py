"""参考图替换规则：夜里睡觉房间用素颜脸，低温日全天用冬装参考图。

纯函数模块：当前时间、当天气温判定结果和图片路径都由调用方传进来；这里
不读文件、不查网络、不写状态，方便单测。

规则来自用户需求：
- 22:00 之后在室内睡觉房间（家/旅店的床、青旅床位）时，脸部参考图换成
  卸妆后的素颜三视图；网吧包厢、洗浴休息厅这类过夜点不算，事件场景不算。
- 每天早上 7 点用所在城市的当日平均气温判一次：低于阈值就全天用冬装
  人物参考图；当天跨城旅行也沿用早上那次判定（判定按「日期」缓存，不再按
  当时所在城市重判）。
"""
import math

from .overnight import OVERNIGHT_SCENES
from .portraits import scene_is_event
from .scene_context import scene_hour

SLEEP_START_HOUR = 22      # 22:00 之后算睡觉时段
SLEEP_END_HOUR = 7         # 次日 07:00 之前还算睡觉时段
BED_TYPES = ("bed", "bed_sleepy")
SLEEPING_ROOM_KINDS = ("bed", "bunk")   # 固定过夜房里真正能睡觉的房间类型
CYBERCAFE_HINT = "网吧"


def _scene_text(scene) -> str:
    scene = scene if isinstance(scene, dict) else {}
    parts = [str(scene.get("desc") or ""), str(scene.get("activity") or "")]
    place = scene.get("place")
    if isinstance(place, dict):
        parts.extend(str(place.get(key) or "") for key in ("name", "place", "desc"))
    return "".join(parts)


def is_cybercafe(scene) -> bool:
    """网吧包厢：过夜不换睡衣，也不换素颜脸（用户明确说网吧不算）。"""
    return CYBERCAFE_HINT in _scene_text(scene)


def is_indoor(scene) -> bool:
    """这一张是不是室内场景（睡觉房间一定在室内）。"""
    if not isinstance(scene, dict):
        return False
    if scene.get("home") is True or str(scene.get("stay") or "") == "home":
        return True
    kind = str(scene.get("type") or "").strip().lower()
    if kind in ("outdoor", "snow", "street", "park", "train", "bus", "station"):
        return False
    return True


def is_sleeping_room(scene) -> bool:
    """室内睡觉房间：床（家里或旅店）或固定过夜房里的床位。

    网吧包厢、洗浴休息厅这类过夜点不是「睡觉房间」；固定过夜房是不是床，
    由 overnight.py 里这间房的 ``sleep`` 字段决定。预览场景（type=lodging）
    同样靠 overnight_id 认出来。
    """
    if not isinstance(scene, dict):
        return False
    kind = str(scene.get("type") or "").strip().lower()
    if kind in BED_TYPES:
        return True
    if not is_indoor(scene) or is_cybercafe(scene):
        return False
    room_id = str(scene.get("overnight_id") or "")
    if not room_id:
        return False
    room = next((row for row in OVERNIGHT_SCENES if row.get("id") == room_id), None)
    return bool(room and room.get("sleep") in SLEEPING_ROOM_KINDS)


def is_night(scene, hour=None) -> bool:
    """22:00~次日 07:00；场景没写时间时退回它自己的 slot。"""
    scene = scene if isinstance(scene, dict) else {}
    if hour is None:
        hour = scene_hour(scene)
    if isinstance(hour, int) and not isinstance(hour, bool):
        return hour >= SLEEP_START_HOUR or hour < SLEEP_END_HOUR
    return str(scene.get("slot") or "") == "sleep"


def bare_face_active(scene, hour=None) -> bool:
    """22 点之后在室内睡觉房间、且不是网吧、不是事件场景 → 换素颜脸部参考图。"""
    scene = scene if isinstance(scene, dict) else {}
    if not is_sleeping_room(scene):
        return False
    if is_cybercafe(scene) or scene_is_event(scene):
        return False
    return is_night(scene, hour)


def winter_decision(mean, threshold=10.0):
    """当日平均气温低于阈值 → 今天换冬装参考图；气温不可用返回 None。

    不把「查不到气温」当成 0℃，避免网络失败时误穿冬装；调用方据此保留
    原来的羽绒服提示词兜底。
    """
    if isinstance(mean, bool) or not isinstance(mean, (int, float)):
        return None
    try:
        value = float(mean)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    try:
        limit = float(threshold)
    except (TypeError, ValueError):
        limit = 10.0
    return value < limit


def winter_active(record, today) -> bool:
    """今天（按中国本地日期）的判定是不是「穿冬装参考图」。"""
    if not isinstance(record, dict):
        return False
    if str(record.get("date") or "") != str(today or ""):
        return False
    return record.get("winter") is True
