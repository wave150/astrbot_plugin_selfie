"""Pure location/time/weather context shared by all image prompt paths."""
from datetime import datetime, timedelta, timezone
import math

LOCAL_ZONE = timezone(timedelta(hours=8), name='Asia/Shanghai')


def local_datetime(timestamp):
    return datetime.fromtimestamp(timestamp, LOCAL_ZONE)


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
