"""Open-Meteo transport and strict current-weather parsing, independently injectable."""
import math
from datetime import datetime, timedelta, timezone

CHINA_TIMEZONE = "Asia/Shanghai"
_CHINA_OFFSET = timezone(timedelta(hours=8), CHINA_TIMEZONE)


def china_local_datetime(timestamp):
    """Domestic simulation wall time, independent of server TZ and tzdata."""
    return datetime.fromtimestamp(timestamp, _CHINA_OFFSET)


async def fetch_current_weather(lat, lon):
    """Return provider JSON; caller owns caching, fallback and location metadata."""
    import aiohttp
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code,is_day"
        "&timezone=Asia%2FShanghai&forecast_days=1"
    )
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


async def fetch_daily_mean(lat, lon):
    """当日平均气温（Open-Meteo daily），只取今天这一天；调用方负责缓存与降级。"""
    import aiohttp
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_mean"
        "&timezone=Asia%2FShanghai&forecast_days=1"
    )
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


def parse_daily_mean(data):
    """当日平均气温；缺数据时抛错，不拿 0℃ 冒充（0℃ 会被当成「今天很冷」）。"""
    daily = data.get("daily") if isinstance(data, dict) else None
    if not isinstance(daily, dict):
        raise ValueError("daily weather must contain daily data")
    values = daily.get("temperature_2m_mean")
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("temperature_2m_mean must be a non-empty list")
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("temperature_2m_mean must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError("temperature_2m_mean must be a finite number") from exc
    if not math.isfinite(value):
        raise ValueError("temperature_2m_mean must be a finite number")
    return round(value, 1)


def parse_current_weather(data):
    """Reject missing data instead of inventing clear weather at zero degrees."""
    current = data.get("current") if isinstance(data, dict) else None
    if not isinstance(current, dict):
        raise ValueError("weather response must contain current conditions")
    temp = current.get("temperature_2m")
    code = current.get("weather_code")
    day = current.get("is_day")
    if isinstance(temp, bool) or not isinstance(temp, (int, float)):
        raise ValueError("temperature_2m must be a finite number")
    try:
        temp = float(temp)
    except OverflowError as exc:
        raise ValueError("temperature_2m must be a finite number") from exc
    if not math.isfinite(temp):
        raise ValueError("temperature_2m must be a finite number")
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 99:
        raise ValueError("weather_code must be an integer in [0, 99]")
    if isinstance(day, bool) or not isinstance(day, int) or day not in (0, 1):
        raise ValueError("is_day must be 0 or 1")
    return {"temp": round(temp, 1), "code": code, "is_day": bool(day)}
