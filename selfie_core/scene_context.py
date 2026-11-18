"""Pure location/time/weather context shared by all image prompt paths."""
from datetime import datetime, timedelta, timezone
import math

LOCAL_ZONE = timezone(timedelta(hours=8), name='Asia/Shanghai')


def local_datetime(timestamp):
    return datetime.fromtimestamp(timestamp, LOCAL_ZONE)


# 时间段 → (说法, 光线和窗外的要求)。夜里那几条专门写死「不要拍成白天」：
# 生图模型默认会把窗户补成白天的自然光，光靠场景描述（哪怕写了「晚上」）压不住。
_LIGHT_TABLE = (
    ((5, 7), "清晨", "天刚亮，窗外是淡青色的晨光，光线柔和偏冷"),
    ((7, 11), "上午", "窗外是明亮的白天日光，室内通透"),
    ((11, 13), "中午", "窗外是正午的日光，光线足、阴影短"),
    ((13, 17), "下午", "窗外是偏暖的午后日光"),
    ((17, 19), "傍晚", "天正在黑，窗外是橙红色的晚霞，室内已经开灯"),
    ((19, 22), "晚上", "窗外已经黑下来，只有街灯和对面楼的灯光，室内开着暖色灯"),
    ((22, 5), "深夜", "窗外是漆黑的夜色和零星的城市灯火，屋里只开着一盏暖色灯，"
                      "照片整体是夜间光线、比白天暗"),
)


def _light_for(hour):
    """(说法, 光线描述, 是不是夜里) —— 拿不到有效小时返回 None。"""
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        return None
    for (start, end), label, light in _LIGHT_TABLE:
        inside = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if inside:
            return label, light, (hour >= 19 or hour < 5)
    return None


def _hour_from_text(value):
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).hour
    except ValueError:
        return None


def scene_hour(scene):
    """这张照片拍下时的小时（场景锁在哪个钟点就按那个钟点）。"""
    scene = scene if isinstance(scene, dict) else {}
    for key in ('local_time', 'time'):
        hour = _hour_from_text(scene.get(key))
        if hour is not None:
            return hour
    key = str(scene.get('hour_key') or '').strip()
    if key:
        try:
            return int(key.split()[-1])
        except (ValueError, IndexError):
            pass
    created = scene.get('created_at')
    if (isinstance(created, (int, float)) and not isinstance(created, bool)
            and math.isfinite(created)):
        try:
            return local_datetime(created).hour
        except (OverflowError, OSError, ValueError):
            pass
    return None


def lighting_text(scene=None, *, hour=None):
    """提示词里的时段/光线约束；拿不到时间就返回空串（不瞎猜）。"""
    if hour is None:
        hour = scene_hour(scene)
    found = _light_for(hour)
    if not found:
        return ''
    label, light, night = found
    if night:
        return (f"此刻是{label}（{hour} 点）：{light}。"
                "窗外如果入镜只能是夜色，照片里不能出现白天的日光。")
    if label == "傍晚":
        return (f"此刻是{label}（{hour} 点）：{light}。"
                "窗外如果入镜是傍晚的晚霞和渐暗的天光，不是正午的白亮日光。")
    return f"此刻是{label}（{hour} 点）：{light}。窗外如果入镜是白天的光线。"


def scene_context_text(scene):
    location = ''.join(str(scene.get(k) or '') for k in ('city', 'district')) or '当前地点'
    local_time = str(scene.get('local_time') or '')
    if not local_time and isinstance(scene.get('created_at'), (float, int)):
        local_time = local_datetime(scene['created_at']).isoformat(timespec='minutes')
    when = f'；当地时间：{local_time}（Asia/Shanghai，UTC+8）' if local_time else ''
    weather = scene.get('weather') or {}
    desc = str(weather.get('desc') or '天气暂不可用')
    if weather.get('available') is False:
        desc = '天气暂不可用，不虚构晴雨或温度'
    else:
        temp = weather.get('temp')
        if isinstance(temp, (int, float)) and not isinstance(temp, bool) and math.isfinite(temp):
            desc += f'，{temp:g}℃'
    return f'地点：{location}{when}；当地天气：{desc}。天气只影响合理的室外或窗外环境，室内不直接下雨下雪。'
