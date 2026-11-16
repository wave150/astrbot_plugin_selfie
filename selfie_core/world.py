"""Legacy world facade: geography, weather and scene progression.
This extraction preserves behaviour; separating policy/IO is a subsequent step.
Host supplies conf/state and _cfg/_save_state/_outfit_name.
"""
import os
import re
import time
import random
from datetime import datetime
from .living import LivingEngine
from .event_materials import active_event, summarize_action, usable_action_text
from .world_editor import province_travel_hours, resolve_city
from astrbot.api import logger
from .geography import WorldProfile, CityCatalog
from .weather import fetch_current_weather, parse_current_weather, china_local_datetime, CHINA_TIMEZONE
from .overnight import OVERNIGHT_SCENES, pick_overnight, scene_by_id
from .defaults import *  # legacy preset constants, deliberately read-only


class WorldNotReady(RuntimeError):
    """No home city / generated world yet; the administrator must set one up."""

    message = "还不知道家在哪个城市呢，请先到工作台「世界设置」里设定家所在城市并生成地点～"


class WorldMixin:
    def _workspace_enabled(self):
        return getattr(self, '_workspace', None) is not None

    def _world_profile(self):
        config = dict(self.conf)
        if self._workspace_enabled():
            config.update(self._workspace_settings())
        return WorldProfile.from_config(config)

    def _city_catalog(self):
        """Only workspace cities exist; the plugin ships no built-in geography."""
        if not self._workspace_enabled():
            return None
        data = {}
        for item in self._workspace_cities().values():
            for city, definition in item.get('data', {}).items():
                previous = data.get(city, {})
                valid = {d: value for d, value in definition.get('districts', {}).items()
                         if isinstance(value, dict) and 'lat' in value and 'lon' in value}
                districts = dict(previous.get('districts', {}), **valid)
                if not districts:
                    continue
                districts = {d: dict(value, neighbors=[n for n in value.get('neighbors', []) if n in districts])
                             for d, value in districts.items()}
                stations = definition.get('stations') or previous.get('stations', [])
                data[city] = {'districts': districts, 'stations': [d for d in stations if d in districts]}
        if not data:
            return None
        catalog = CityCatalog.from_dict(data)
        catalog.validate_profile(self._world_profile())
        return catalog

    def _world_ready(self) -> bool:
        """True once a home city exists and has usable district coordinates."""
        profile = self._world_profile()
        if not profile.configured:
            return False
        catalog = self._city_catalog()
        if catalog is None or profile.home_city not in catalog.city_names():
            return False
        return profile.home_district in catalog.district_names(profile.home_city)

    def _current_city(self):
        return str(self.state.get("city") or self._world_profile().home_city)

    def _trip_destination(self, city):
        profile = self._world_profile()
        if not profile.configured:
            return ""
        if city != profile.home_city:
            return profile.home_city
        catalog = self._city_catalog()
        if catalog is None:
            return ""
        choices = [x for x in profile.travel_cities if catalog.has_route(city, x)]
        return self.rng.choice(choices) if choices else ""

    def _district_coords(self, district: str) -> tuple[float, float] | None:
        """Coordinates come from the generated catalog only; unknown means no weather."""
        catalog = self._city_catalog()
        name = str(district or "").strip() or self._world_profile().home_district
        if not catalog or not name:
            return None
        city = self._current_city()
        try:
            return catalog.coords(city, name)
        except ValueError:
            home = self._world_profile()
            if home.configured:
                try:
                    return catalog.coords(home.home_city, home.home_district)
                except ValueError:
                    return None
            return None

    def _district_adjacency(self) -> dict:
        """{区名: 相邻区元组} 只来自当前城市生成的拓扑；没有城市时为空。"""
        catalog = self._city_catalog()
        if not catalog:
            return {}
        city = self._current_city()
        if city not in catalog.city_names():
            city = self._world_profile().home_city
        if not city:
            return {}
        return {name: catalog.neighbors(city, name) for name in catalog.district_names(city)}


    def _district_neighbors(self, district: str) -> set[str]:
        """当前区 + 相邻区（双向认）。换场景只能在这个范围内挑地点。"""
        name = str(district or "").strip() or self._world_profile().home_district
        near = self._district_adjacency()
        allowed = {name}
        allowed |= set(near.get(name) or ())
        for other, nbs in near.items():
            if name in nbs:
                allowed.add(other)
        return allowed


    def _world_local_datetime(self, timestamp=None):
        return china_local_datetime(self.clock.time() if timestamp is None else timestamp)

    def _world_localtime(self, timestamp=None):
        if self._workspace_enabled():
            return self._world_local_datetime(timestamp).timetuple()
        return self.clock.localtime(self.clock.time() if timestamp is None else timestamp)

    def _world_strftime(self, fmt, timestamp=None):
        return self.clock.strftime(fmt, self._world_localtime(timestamp))

    async def _weather(self, district: str = "", city: str | None = None, *, persist=True) -> dict:
        """City-scoped cache; weather_fetcher(lat, lon) is an injectable async boundary."""
        city = str(city or self._current_city())
        name = str(district or "").strip() or self._world_profile().home_district
        now = self.clock.time()
        store = self.state.get("weather_by_city")
        store = dict(store) if isinstance(store, dict) else {}
        city_store = store.get(city)
        city_store = dict(city_store) if isinstance(city_store, dict) else {}
        cached = city_store.get(name) or {}
        lat = lon = None
        try:
            catalog = self._city_catalog()
            if not district and catalog and city != self._world_profile().home_city:
                name = catalog.district_names(city)[0]
                cached = city_store.get(name) or {}
            lat, lon = (catalog.coords(city, name) if catalog else self._district_coords(name)) or (None, None)
            if lat is None or lon is None:
                raise ValueError("该城市和区还没有坐标，暂时无法查天气")
            try:
                valid_cache = (cached.get("available") is True and cached.get("city") == city
                               and cached.get("district") == name and cached.get("lat") == lat
                               and cached.get("lon") == lon and cached.get("timezone") == CHINA_TIMEZONE
                               and 0 <= now - float(cached.get("ts", 0)) < WEATHER_CACHE_SECONDS)
            except (AttributeError, TypeError, ValueError):
                valid_cache = False
            if valid_cache:
                weather = dict(cached)
                if persist:
                    self.state["weather"] = weather
                return weather
            fetcher = getattr(self, "weather_fetcher", None)
            if fetcher is None:
                fetcher = fetch_current_weather
            current = parse_current_weather(await fetcher(lat, lon))
            code = current["code"]
            weather = {
                **current, "ts": now, "city": city, "district": name,
                "lat": lat, "lon": lon, "timezone": CHINA_TIMEZONE,
                "fetched_at": self._world_local_datetime(now).isoformat(),
                "desc": WMO_DESC.get(code, f"天气代码{code}"),
                "snow": code in SNOW_CODES, "clear": code in CLEAR_CODES,
                "rain": code in RAIN_CODES, "available": True,
            }
            if persist:
                city_store[name] = weather
                store[city] = city_store
                self.state["weather_by_city"] = store
                self.state["weather"] = weather
                self._save_state()
            logger.info(f"[selfie] {city}/{name}天气: {weather['desc']} {weather['temp']}℃")
            return weather
        except Exception as e:
            logger.warning(f"[selfie] {city}/{name}天气查询失败: {e}")
            weather = {
                "ts": now, "city": city, "district": name, "available": False,
                "code": None, "desc": "天气暂不可用", "temp": None,
                "is_day": None, "snow": False, "clear": False, "rain": False,
                "lat": lat, "lon": lon, "timezone": CHINA_TIMEZONE,
                "fetched_at": self._world_local_datetime(now).isoformat(),
            }
            if persist:
                self.state["weather"] = weather
            return weather


    @staticmethod
    def _time_slot(hour: int) -> str:
        """07:00-18:00 出门；18:00-22:00 在家；22:00-次日 07:00 睡觉。"""
        if hour >= 22 or hour < 7:
            return "sleep"
        if hour >= 18:
            return "home_evening"
        return "out"


    @staticmethod
    def _bed_subtype(hour: int, minute: int) -> str:
        """22:00~次日 00:30 普通睡衣自拍；00:30~07:00 裹被子发困。"""
        if hour < 7 and (hour >= 1 or minute >= 30):
            return "bed_sleepy"
        return "bed"


    def _pick_activity(self, weather: dict, hour: int, weekday: int,
                       activities: list | None = None) -> dict:
        """白天去哪：下雨下雪只在室内；工作日白天有 20% 去学校；其余随机。

        ``activities`` 是当前城市生成的地点；为空（世界还没配置）时返回空，
        由调用方退回家居场景，不再内置任何城市的地点。
        """
        acts = [
            a for a in (activities or ())
            if str(a.get("kind")) in ("outdoor", "indoor", "school")
        ]
        if not acts:
            return {}
        rainy = weather.get("snow") or weather.get("rain")
        if rainy:
            return dict(self.rng.choice([a for a in acts if a.get("kind") == "indoor"] or acts))
        is_workday = weekday not in (0, 6)  # 周一~周五
        school_candidates = [a for a in acts if a.get("kind") == "school"]
        if is_workday and 8 <= hour <= 17 and school_candidates and self.rng.random() < 0.2:
            return dict(self.rng.choice(school_candidates))
        if weather.get("clear"):
            pool_ = [a for a in acts if a.get("kind") == "outdoor"]
            mixed = pool_ * 2 + [a for a in acts if a.get("kind") == "indoor"]
            return dict(self.rng.choice(mixed or acts))
        return dict(self.rng.choice([a for a in acts if a.get("kind") != "school"] or acts))


    @staticmethod
    def _home_activity_tables() -> tuple[list, dict]:
        """家居活动来自通用预设；城市地点只来自生成的世界数据。"""
        acts = []
        for home in HOME_SCENES:
            acts.append({
                "name": home.get("name", ""),
                "place": home.get("desc", ""),
                "kind": f"home_{home.get('slot', 'evening')}",
            })
        return acts, dict(SCENE_TEMPLATES)


    def _scene_tables(self) -> tuple[list, dict]:
        """(地点, 文案模板)：地点只来自 workspace 里生成的城市。"""
        activities, templates = self._home_activity_tables()
        activities = list(activities)
        for item in self._workspace_cities().values():
            activities.extend(dict(a) for a in item.get('activities', []))
        # Event venues are not ordinary destinations, even outside event hours.
        reserved = set()
        for event in self._workspace_events():
            for part in [event, {'scene': event.get('end_scene')}] + list(event.get('phases', [])):
                value = part.get('scene')
                if isinstance(value, str):
                    reserved.add((event.get('city'), event.get('district'), value))
                elif isinstance(value, dict):
                    for key in ('name', 'place', 'desc'):
                        if value.get(key):
                            reserved.add((event.get('city'), event.get('district'), value[key]))
        activities = [a for a in activities
                      if not any((a.get('city'), a.get('district'), a.get(key)) in reserved for key in ('name', 'place'))
                      and not a.get('event_only') and not a.get('event_id')
                      and a.get('kind') not in ('event', 'club', 'dashanghai', 'lodging')]
        return activities, templates

    @staticmethod
    def _render_scene(templates: dict, kind: str, **fields: object) -> str:
        tpl = str(templates.get(kind) or SCENE_TEMPLATES.get(kind) or "")
        try:
            return tpl.format(**fields)
        except Exception:
            return tpl


    @staticmethod
    def _scene_caption_tail(scene: dict) -> str:
        cap = str(scene.get("caption") or "").strip()
        if cap:
            return cap
        activity = str(scene.get("activity") or "")
        # 事件动作正文是长篇生图提示词：配文只留短概括。旧缓存/异常数据也会在这里兜住，
        # 保证「发图时带一大段提示词」不会再发生。
        if len(activity.strip()) > 24:
            short = usable_action_text(summarize_action(activity))
            return f"在{short}" if short else "在外面"
        # 活动名本身就带「在/去」时别再拼一层，避免「在在某某的酒店住下」
        if activity.startswith(("在", "去", "准备去", "我要去", "我要回")):
            return activity
        return f"在{activity or '某处'}"


    @staticmethod
    def _scene_text(scene: dict) -> str:
        """把场景里所有「能看出在哪」的文字拼起来，用来判断网吧/火车站这类特殊地点。"""
        parts = [str(scene.get("desc") or ""), str(scene.get("activity") or "")]
        place = scene.get("place")
        if isinstance(place, dict):
            parts.append(str(place.get("name") or ""))
            parts.append(str(place.get("place") or ""))
        return "".join(parts)


    @classmethod
    def _is_cybercafe_scene(cls, scene: dict) -> bool:
        """是不是在网吧包厢（网吧过夜不换睡衣，就穿着白天的衣服）。"""
        return "网吧" in cls._scene_text(scene)


    @staticmethod
    def _scene_hour(scene: dict) -> int:
        """场景锁定时的钟点（scene["hour_key"] = "YYYY-MM-DD HH"），拿不到就用当前小时。"""
        try:
            tail = str(scene.get("hour_key") or "").split(" ")[-1]
            return int(tail.split(":")[0])
        except Exception:
            return china_local_datetime(time.time()).hour


    def _night_scene_kind(self, scene: dict) -> str:
        """入睡后（00:30~07:00）这一张该怎么拍：


        "bunk"  青旅/上下铺 → 躺在架子床上举着镜头自拍
        "bed"   普通床     → 侧躺、被子裹到下巴
        "cyber" 网吧包厢    → 趴在桌子上、外套披在背上
        ""      不是入睡状态，走原来的普通自拍
        三种都要关灯，只留手机屏幕的光。
        """
        st = str(scene.get("type") or "")
        if st == "bed_sleepy":
            text = self._scene_text(scene)
            if any(k in text for k in ("青旅", "上下铺", "铁架床", "架子床", "高低床", "床位")):
                return "bunk"
            return "bed"
        if self._is_cybercafe_scene(scene):
            hour = self._scene_hour(scene)
            now_hour = int(self._world_localtime().tm_hour)
            minute = int(self._world_localtime().tm_min) if hour == now_hour else 0
            if self._bed_subtype(hour, minute) == "bed_sleepy":
                return "cyber"
        return ""


    @staticmethod
    def _is_station_place(place: dict) -> bool:
        """这个地点是不是火车站/站前广场。"""
        text = "".join(
            str(place.get(k) or "") for k in ("name", "place", "desc")
        )
        return any(hint in text for hint in STATION_PLACE_HINTS)


    def _scene_location_text(self) -> str:
        """「你现在在哪」的口语说法：家 / 某城的酒店 / 网吧包厢。"""
        scene = self.state.get("scene") if isinstance(self.state.get("scene"), dict) else {}
        plan = self.state.get("night_plan") if isinstance(self.state.get("night_plan"), dict) else {}
        city = str(scene.get("city") or self.state.get("city") or self._world_profile().home_city)
        text = self._scene_text(scene)
        if "网吧" in text:
            return "网吧包厢" if city == self._world_profile().home_city else f"{city}的网吧包厢"
        if str(plan.get("stay") or "") == "lodging" or city != self._world_profile().home_city:
            return f"{city}的酒店"
        return "家"


    def _next_plan_text(self, hour: int, scene: dict | None = None) -> str:
        """接下来那个「已经定下来」的安排：下一站 / 坐车 / 今晚住哪。


        没有明确安排就返回空字符串（别让她编一个没定的事）。
        """
        scene = scene if isinstance(scene, dict) else {}
        pending = self.state.get("pending_target")
        if isinstance(pending, dict) and pending.get("name"):
            name = str(pending.get("name") or "")
            return f"下一站{'' if name.startswith('去') else '去'}{name}"
        trip = self.state.get("trip") if isinstance(self.state.get("trip"), dict) else {}
        to_city = str(trip.get("to") or "")
        if to_city:
            if str(trip.get("stage") or "") == "wait" or str(scene.get("type") or "") == "station":
                return f"在车站等车去{to_city}"
            return f"接下来要坐车去{to_city}"
        plan = self.state.get("night_plan") if isinstance(self.state.get("night_plan"), dict) else {}
        if str(plan.get("night") or "") == self._night_key(self.clock.time()):
            stay = str(plan.get("stay") or "")
            if stay == "lodging":
                place = plan.get("place") if isinstance(plan.get("place"), dict) else {}
                name = str(place.get("name") or "")
                return f"今晚{name}" if name else "今晚找个地方住下"
            if stay == "home":
                return "今晚回家"
        return ""


    def _pick_place(self, activities: list, city: str, allowed: set,
                    kinds: tuple = ("outdoor", "indoor", "school"),
                    exclude: set | None = None,
                    stay_in_allowed: bool = False,
                    allow_station: bool = False) -> dict | None:
        """在 allowed（当前区+相邻区）里挑一个地点。


        exclude：今天已经去过的地方，优先跳过。
        stay_in_allowed=True：绝不越出 allowed（车站目的地用；范围内全去过就
        允许重复，而不是跑到别的区）。默认则在本范围逛遍后退到全城没去过的地点。
        allow_station=True：允许挑到火车站/站前广场。只有「下一秒真要跨城、
        这一小时是往车站走一站」时才传 True；平时不传，避免无缘无故 roll 到火车站。
        """
        pool = [
            a for a in activities
            if str(a.get("city") or self._world_profile().home_city) == city and str(a.get("kind")) in kinds
        ]
        if not allow_station:
            pool = [a for a in pool if not self._is_station_place(a)] or pool
        near = [a for a in pool if str(a.get("district") or self._world_profile().home_district) in allowed]
        choose_from = near or pool
        if exclude:
            fresh = [a for a in choose_from if str(a.get("name") or "") not in exclude]
            if not fresh and not stay_in_allowed:
                # 当前范围今天逛遍了：退到全城还没去过的地方，别重复
                fresh = [a for a in pool if str(a.get("name") or "") not in exclude]
            choose_from = fresh or choose_from
        return dict(self.rng.choice(choose_from)) if choose_from else None


    def _club_outfit(self) -> dict:
        """夜店里这身打扮，整晚固定（记在 state 里，换小时不跳）。"""
        cur = self.state.get("club_outfit")
        if isinstance(cur, dict) and cur.get("name"):
            return dict(cur)
        picked = dict(self.rng.choice(CLUB_OUTFITS))
        self.state["club_outfit"] = picked
        return picked


    def _is_club_time(self, hour: int) -> bool:
        """22:00 ~ 次日 07:00 的夜间时段。"""
        return hour >= 22 or hour < 7


    def _night_plan(self) -> dict | None:
        """今晚（18:00 ~ 次日 07:00）已经定下的安排：住宿 或 事件。


        18 点判定一次就写进 state，整晚不再改；换晚（_night_key 变了）自动作废。
        """
        key = self._night_key(self.clock.time())
        rec = self.state.get("night_plan")
        if isinstance(rec, dict) and rec.get("night") == key:
            return rec
        changed = False
        if rec is not None:
            self.state.pop("night_plan", None)
            changed = True
        # 兼容老字段 night_event：只保留城市/区/住宿形态，旧的内置事件名作废
        old = self.state.get("night_event")
        if isinstance(old, dict):
            self.state.pop("night_event", None)
            changed = True
            if old.get("night") == key:
                district = str(old.get("district") or self._world_profile().home_district)
                city = str(self.state.get("city") or self._world_profile().home_city)
                plan = {
                    "night": key,
                    "kind": "stay",
                    "name": "",
                    "district": district,
                    "city": city,
                    "stay": self._stay_kind(city, district),
                    "place": None,
                }
                if plan["stay"] == "lodging":
                    plan["overnight_id"] = pick_overnight(self.rng)["id"]
                self.state["night_plan"] = plan
                self._save_state()
                return plan
        if changed:
            self._save_state()
        return None


    def _stay_kind(self, city: str, district: str) -> str:
        """住宿的形态：离家常住的区近就回家，其余一律在外过店。"""
        if city != self._world_profile().home_city:
            return 'lodging'
        near_home = district in (
            self._district_neighbors(self._world_profile().home_district) | {self._world_profile().home_district}
        )
        return "home" if (city == self._world_profile().home_city and near_home) else "lodging"


    def _decide_night_plan(self, hour: int, city_now: str, district: str,
                           activities: list) -> dict:
        """18 点那一刻的唯一一次判定：住宿。整晚沿用同一间固定过夜房。

        特殊夜间活动由工作台里的事件负责；这里不再内置任何城市的夜间事件。
        住宿地点在这里就定死（连住哪家店也别再变）。
        """
        key = self._night_key(self.clock.time())
        plan = {
            "night": key,
            "kind": "stay",
            "name": "",
            "district": district,
            "city": city_now,
            "decided_at": self.clock.time(),
        }
        stay = self._stay_kind(city_now, district)
        plan["stay"] = stay
        if stay == "lodging":
            # Ten fixed overnight rooms are rolled once and reused for the
            # whole night; they never enter the ordinary place pool.
            plan["overnight_id"] = pick_overnight(self.rng)["id"]
        plan["place"] = None
        self.state["night_plan"] = plan
        self._save_state()
        logger.info(
            f"[selfie] {key} 这一晚判定：住宿（{plan.get('stay')}）（{city_now}/{district}）"
        )
        return plan


    @staticmethod
    def _event_name_at(plan: dict, hour: int) -> str:
        """这一晚是否还有效的事件名（旧版本迁移过来的名字一律不再使用）。"""
        return ""


    def _night_phase(self, plan: dict, hour: int, bed_type: str) -> str:
        """今晚的「阶段」：只有阶段变了才重新生成场景，其余时候原样复用。"""
        slot = self._time_slot(hour)
        if str(plan.get("stay") or "") == "home":
            return f"stay_home_{bed_type}" if slot == "sleep" else "stay_home_evening"
        return f"stay_lodging_{bed_type}" if slot == "sleep" else "stay_lodging_evening"


    def _night_scene_for(self, plan: dict, hour: int, bed_type: str,
                         activities: list, templates: dict,
                         season: str, weather: dict) -> dict:
        """按这一晚的最终安排生成场景（住宿），地点整晚不变。"""
        district = str(plan.get("district") or self._world_profile().home_district)
        city = str(plan.get("city") or self._world_profile().home_city)
        slot = self._time_slot(hour)
        if str(plan.get("stay") or "") == "home":
            return self._home_scene(slot if slot == "sleep" else "home_evening",
                                    bed_type, activities, templates, season, weather)
        fixed = scene_by_id(plan.get("overnight_id"), self.rng)
        plan["overnight_id"] = fixed["id"]
        scene = self._lodging_scene(city, district, activities, season, weather, fixed=fixed)
        # 在外住店过夜：床和上下铺都进入睡眠镜头；网吧/休息厅保持白天姿势。
        if slot == "sleep":
            sleep_kind = str(fixed.get("sleep") or "bed")
            if sleep_kind in ("bed", "bunk"):
                kind = "bed_sleepy" if bed_type == "bed_sleepy" else "bed"
                scene["type"] = kind
                scene["activity"] = "裹着被子睡觉" if kind == "bed_sleepy" else "准备睡觉"
            elif sleep_kind == "cyber":
                scene["activity"] = "在网吧包厢趴着过夜"
            else:
                scene["activity"] = "在休息厅躺椅上过夜"
        return scene


    def _place_scene(self, place: dict, templates: dict, weather: dict, season: str,
                     weather_desc: str, temp: float, hour: int, city: str) -> dict:
        kind = str(place.get("kind") or "indoor")
        tpl_kind = kind if kind in ("outdoor", "indoor", "school") else "indoor"
        desc = self._render_scene(templates, tpl_kind, place=place.get("place") or "",
                                   weather=weather_desc, season=season,
                                   temp=temp if weather.get('temp') is not None else '未知')
        name = str(place.get("name") or "")
        # 下午的南操场：有电子音乐在放
        if "南操场" in name and 12 <= hour < 18:
            desc += "；这会儿操场上好像有放电子音乐的声音，鼓点一下一下地传过来"
        return {
            "type": kind,
            "desc": desc,
            "activity": name,
            "district": str(place.get("district") or self._world_profile().home_district),
            "city": city,
            "weather": weather,
        }


    def _transit_scene(self, kind: str, caption: str, city: str, district: str,
                       season: str, weather: dict) -> dict:
        base = TRANSIT_DESC.get(kind) or TRANSIT_DESC["metro"]
        return {
            "type": kind,
            "desc": f"{base}，{season}" if season else base,
            "activity": caption,
            "caption": caption,
            "district": district,
            "city": city,
            "weather": weather,
        }


    def _pick_lodging(self, city: str, activities: list) -> dict:
        """Ten fixed rooms; legacy activities never supply overnight places."""
        return pick_overnight(self.rng)


    def _lodging_scene(self, city: str, district: str, activities: list,
                       season: str, weather: dict, fixed: dict | None = None) -> dict:
        """Fixed room text + the actual city/district/weather of tonight."""
        room = fixed if isinstance(fixed, dict) and fixed.get("desc") else pick_overnight(self.rng)
        return {
            "type": "lodging",
            "home": False,
            "tags": ["lodging"],
            "desc": str(room.get("desc") or ""),
            "activity": str(room.get("name") or "在旅馆住下"),
            "district": district or self._world_profile().home_district,
            "city": city,
            "weather": weather,
        }


    def _move_to_place(self, place: dict, prev_district: str, city_now: str,
                       templates: dict, weather: dict, season: str,
                       weather_desc: str, temp: float, hour: int,
                       allow_transit: bool = True) -> dict:
        """同城移动一幕：跨区有概率先坐地铁/公交（下一幕到 pending），否则直接到场。


        allow_transit=False：这一小时之后就要入夜（18点必须室内），不发起跨小时的
        地铁/公交，直接到场地，避免下一幕在夜里才"到站"。
        """
        cross = str(place.get("district") or self._world_profile().home_district) != prev_district
        if cross and allow_transit and self.rng.random() < MOVE_TRANSIT_PROB:
            vehicle = "metro" if self.rng.random() < 0.6 else "bus"
            # 地点名本身就以「去」开头（去二七广场…），别再拼成「准备去去…」
            target_name = str(place.get("name") or "下个地方")
            if target_name.startswith("去"):
                target_name = target_name[1:]
            caption = f"准备去{target_name}"
            scene = self._transit_scene(vehicle, caption, city_now,
                                         prev_district, season, weather)
            self.state["pending_target"] = place
            return scene
        return self._place_scene(place, templates, weather, season,
                                 weather_desc, temp, hour, city_now)


    def _can_board_now(self, hour: int) -> bool:
        """每小时整点是否允许发车（高铁班次窗口）。"""
        return BOARD_FROM_HOUR <= hour <= BOARD_TO_HOUR


    def _wants_new_trip(self, city_now: str, hour: int, now: float) -> bool:
        """这一整点要不要开一段新的城际行程。

        必须在「车站小时」内开票：车站小时 H 对应 H+1 点发车，
        所以 H 最晚是 BOARD_TO_HOUR - 1，最早的班次从 BOARD_FROM_HOUR 起。
        """
        profile = self._world_profile()
        if not profile.configured:
            return False
        if city_now == profile.home_city and not profile.travel_cities:
            return False
        if not self._can_board_now(hour + 1):
            return False
        since = self.state.setdefault('city_since', {})
        since.setdefault(city_now, now)
        settings = self._workspace_settings()
        item = self._workspace_cities().get(city_now, {})
        minimum = settings.get('city_min_days', {}).get(
            city_now, item.get('min_stay_days', 10 if city_now == profile.home_city else 2.5))
        if now - since[city_now] < float(minimum) * 86400:
            return False
        if city_now != profile.home_city:
            return bool(self._trip_destination(city_now))
        today = self._world_strftime('%Y-%m-%d')
        plan = self.state.get('city_plan') or {}
        if plan.get('date') != today:
            plan = {'date': today, 'go': self.rng.random() < .35, 'done': False}
            self.state['city_plan'] = plan
        if not plan.get('go') or plan.get('done'):
            return False
        plan['done'] = True
        return bool(self._trip_destination(city_now))


    def _hit_station_today(self, prev_district: str, place: dict, city: str,
                           hour: int) -> bool:
        """这一小时「往车站走一站」之后，下一小时还来不来得及成为一个车站小时。

        条件：落脚点落在车站片区（下一小时能直接是车站），
        且下一小时的下一小时还能发车（H < BOARD_TO_HOUR）。
        """
        if hour + 1 > BOARD_TO_HOUR - 1:
            return False
        return str(place.get("district") or self._world_profile().home_district) in self._station_districts(city)


    def _station_places(self, city: str) -> list[dict]:
        """这座城市生成时写下的「车站」地点；没有就用通用候车描述。"""
        return [a for a in self._scene_tables()[0]
                if str(a.get("city") or "") == city
                and str(a.get("kind") or "") == "station"]


    def _station_scene(self, city: str, district: str, season: str, weather: dict) -> dict:
        """这一天在车站候车的场景（type=station，锁一个整点）。"""
        places = self._station_places(city)
        chosen = next((a for a in places
                       if str(a.get("district") or "") == district), None) or (places[0] if places else {})
        name = str(chosen.get("name") or f"在{city}的车站候车")
        place = str(chosen.get("place") or "车站的候车大厅")
        desc = f"{place}，人来人往、行李堆在脚边，检票口的电子屏在翻班次"
        return {
            "type": "station",
            "desc": f"{desc}，{season}" if season else desc,
            "activity": name,
            "district": district or self._world_profile().home_district,
            "city": city,
            "weather": weather,
        }


    def _station_districts(self, city: str) -> set[str]:
        """赶火车前/下车后允许所在的区：城市生成时配置的 stations。"""
        catalog = self._city_catalog()
        if not catalog or city not in catalog.city_names():
            return set()
        return set(catalog.station_districts(city))
    def _next_hop_towards(self, start: str, targets: set[str], city: str,
                          activities: list) -> str | None:
        """在邻接图上朝目标片区走一步，返回下一站区名（必须有可去场景）。"""
        if not start or start in targets:
            return None
        with_places = {
            str(a.get("district") or self._world_profile().home_district)
            for a in activities
            if str(a.get("city") or self._world_profile().home_city) == city
            and str(a.get("kind")) != "lodging"
        }
        if start not in with_places:
            with_places.add(start)
        graph = self._district_adjacency()
        front, seen = [(start, None)], {start}
        while front:
            nxt = []
            for cur, first in front:
                nbs = set(graph.get(cur) or ())
                nbs |= {o for o, ns in graph.items() if cur in ns}
                for n in sorted(nbs):
                    if n in seen:
                        continue
                    seen.add(n)
                    hop = first or n
                    if n in targets and n in with_places:
                        return hop
                    if n in with_places:
                        nxt.append((n, hop))
            front = nxt
        return None


    def _visited_today(self) -> set[str]:
        """今天已经去过的地点名（跨城市共用一份）。"""
        today = self._world_strftime("%Y-%m-%d")
        rec = self.state.get("visited_today")
        if not isinstance(rec, dict) or rec.get("date") != today:
            return set()
        return {str(x) for x in (rec.get("places") or []) if x}


    def _record_visit(self, name: str) -> None:
        if not name:
            return
        today = self._world_strftime("%Y-%m-%d")
        rec = self.state.get("visited_today")
        if not isinstance(rec, dict) or rec.get("date") != today:
            rec = {"date": today, "places": []}
        places = [str(x) for x in (rec.get("places") or []) if x]
        if name not in places:
            places.append(name)
        self.state["visited_today"] = {"date": today, "places": places[-80:]}


    def _home_scene(self, slot: str, bed_type: str, activities: list, templates: dict,
                    season: str, weather: dict) -> dict:
        if slot == "sleep":
            kind = "bed_sleepy" if bed_type == "bed_sleepy" else "bed"
            return {
                "type": kind,
                "desc": self._render_scene(templates, kind, season=season),
                "activity": "裹着被子睡觉" if kind == "bed_sleepy" else "准备睡觉",
                "district": self._world_profile().home_district,
                "city": self._world_profile().home_city,
                "weather": weather,
            }
        home_evening = [a for a in activities if str(a.get("kind")) == "home_evening"]
        home = dict(self.rng.choice(home_evening)) if home_evening else {"name": "在家", "place": "家里"}
        return {
            "type": "home_evening",
            "desc": self._render_scene(templates, "home_evening",
                                       place=home.get("place") or home.get("desc") or "",
                                       season=season),
            "activity": home.get("name") or "",
            "district": self._world_profile().home_district,
            "city": self._world_profile().home_city,
            "weather": weather,
        }


    def _travel_hours(self, origin, destination):
        cities = self._workspace_cities()
        def province(city):
            known = cities.get(city, {}).get('province')
            if known:
                return known
            try:
                return resolve_city(city).province
            except ValueError:
                return ""
        origin_province, dest_province = province(origin), province(destination)
        if not origin_province or not dest_province:
            return 2
        return province_travel_hours(origin_province, dest_province)

    async def _workspace_commit_scene(self, scene, now, engine):
        local = self._world_localtime(now)
        scene['created_at'] = now
        scene['local_time'] = self._world_local_datetime(now).isoformat()
        scene['timezone'] = CHINA_TIMEZONE
        scene['hour_key'] = self.clock.strftime('%Y-%m-%d %H', local)
        scene['slot'] = self._time_slot(local.tm_hour)
        scene['locked_until'] = now + SCENE_LOCK_SECONDS
        scene['weather'] = await self._weather(scene['district'], scene['city'])
        scene.setdefault('outfit', self._outfit_name(scene))
        scene['can_change_clothes'] = bool(scene.get('home') or scene.get('type') == 'lodging' or 'lodging' in scene.get('tags', []))
        self.state['scene'] = scene
        self.state['city'] = scene['city']
        engine.record(datetime(*local[:6]), scene)
        self._save_state()
        return scene

    def _scene_reusable(self, now, dt, previous) -> bool:
        """整点闸门：同一钟点内复用已生成的场景。

        后台生活循环每 30 秒调一次 ``_get_scene()``；如果每次都重新掷地点，日程
        会变成每分钟一个观察点、角色 30 秒换一个地方。这里和聊天侧一样按整点
        推进，但放行钟点内真正会发生的切换：到站、事件开始/阶段/结束。
        """
        if not isinstance(previous, dict) or not previous.get('desc'):
            return False
        hour_key = self.clock.strftime('%Y-%m-%d %H', self._world_localtime(now))
        if str(previous.get('hour_key') or '') != hour_key:
            return False
        # 00:30 是「关灯睡觉」的分界点，落在同一个钟点里也必须换一次场景。
        if previous.get('type') in ('bed', 'bed_sleepy'):
            expected = ('bed_sleepy' if self._bed_subtype(dt.hour, dt.minute) == 'bed_sleepy'
                        else 'bed')
            if previous.get('type') != expected:
                return False
        pending = self.state.get('pending_target')
        if isinstance(pending, dict) and pending.get('arrive_at'):
            try:
                if now >= float(pending['arrive_at']):
                    return False
            except (TypeError, ValueError):
                pass
        active = active_event(self.state)
        event = active.get('event') if active else None
        if isinstance(event, dict):
            try:
                start = datetime.fromisoformat(str(active.get('start')))
                end = datetime.fromisoformat(str(active.get('end')))
            except (TypeError, ValueError):
                return True
            phase = str(previous.get('living_phase') or '')
            if dt >= end:
                return False            # 事件结束（可能落在钟点中间）
            if phase == 'prepare' and dt >= start:
                return False            # 钟点中间开演
            if phase == 'active':
                offset = (dt - start).total_seconds() / 3600
                expected = None
                for index, part in enumerate(event.get('phases') or []):
                    try:
                        if float(part['start']) <= offset < float(part['end']):
                            expected = index
                            break
                    except (KeyError, TypeError, ValueError):
                        continue
                if expected is not None and previous.get('phase_index') != expected:
                    return False        # 阶段在钟点中间切换
        return True

    async def _get_scene(self) -> dict:
        """Current scene. Raises WorldNotReady until a home city has been set up."""
        if not self._world_ready():
            raise WorldNotReady()
        now = self.clock.time()
        local = self._world_localtime(now)
        dt = datetime(*local[:6])
        engine = LivingEngine(self.state, self.rng)
        profile = self._world_profile()
        city = self._current_city()
        previous = self.state.get('scene') or {}
        catalog = self._city_catalog()
        district = previous.get('district') or (profile.home_district if city == profile.home_city else catalog.district_names(city)[0])
        since = self.state.setdefault('city_since', {})
        since.setdefault(city, now)
        # 同一钟点内轮询不再换地方：否则日程会一分钟一个观察点。
        if self._scene_reusable(now, dt, previous):
            return dict(previous)
        pending = self.state.get('pending_target') or {}
        if pending.get('arrive_at') and now < float(pending['arrive_at']):
            scene = self._transit_scene('train', f"正在前往{pending['city']}", city, district, '', {})
            return await self._workspace_commit_scene(scene, now, engine)
        if pending.get('arrive_at'):
            destination = pending['city']
            self.state.pop('pending_target', None)
            self.state.pop('trip', None)
            self.state.pop('trip_intent', None)
            self.state.pop('night_plan', None)
            self.state['city'] = destination
            since[destination] = now
            scene = {'type': pending.get('kind', 'station'), 'desc': pending.get('place', '抵达目的地车站'),
                     'activity': pending.get('name', '到站'), 'city': destination, 'district': pending['district']}
            if local.tm_hour >= 18 or local.tm_hour < 7:
                activities, templates = self._scene_tables()
                plan = self._decide_night_plan(local.tm_hour, destination, pending['district'], activities)
                scene = self._night_scene_for(plan, local.tm_hour, self._bed_subtype(local.tm_hour, local.tm_min),
                                              activities, templates, '', {})
                scene['home'] = plan.get('stay') == 'home'
                if scene['home']:
                    scene['tags'] = ['home']
            return await self._workspace_commit_scene(scene, now, engine)
        traveling = bool(pending or self.state.get('trip') or self.state.get('trip_intent'))
        activities, templates = self._scene_tables()
        season = SEASON_HINT.get(int(local.tm_mon), "")
        # 城际旅行：先决定要不要出门，再按「先去车站、下一个整点上车」推进。
        scene = await self._travel_scene(now, local, city, district, activities, templates, season)
        if scene:
            return await self._workspace_commit_scene(scene, now, engine)
        home_neighbors = set(catalog.neighbors(profile.home_city, profile.home_district))
        home_neighbors |= {d for d in catalog.district_names(profile.home_city)
                           if profile.home_district in catalog.neighbors(profile.home_city, d)}
        settings = self._workspace_settings()
        scene = engine.scene(dt, city=city, district=district, home_city=profile.home_city,
                             home_district=profile.home_district, neighbors=self._district_neighbors(district),
                             home_neighbors=home_neighbors, events=self._workspace_events(), traveling=traveling,
                             stay_home_probability=settings.get('stay_home_probability', .5),
                             home_rooms=settings.get('home_rooms', {}))
        if scene:
            self.state.pop('night_plan', None)
            return await self._workspace_commit_scene(scene, now, engine)
        # No scheduled activity: fall back to the current night plan or the home room,
        # never to a built-in city.
        if (self.state.get('night_plan') or {}).get('kind') == 'event':
            self.state.pop('night_plan', None)
            self.state.pop('scene', None)
        if previous.get('living_phase'):
            # An event boundary can fall within the same wall-clock hour.
            self.state['scene'] = dict(previous, hour_key='')
        hour = int(local.tm_hour)
        slot = self._time_slot(hour)
        bed_type = self._bed_subtype(hour, int(local.tm_min))
        weather = await self._weather(district, city)
        if slot in ('sleep', 'home_evening'):
            plan = self._night_plan() or self._decide_night_plan(hour, city, district, activities)
            scene = self._night_scene_for(plan, hour, bed_type, activities, templates, season, weather)
            scene['home'] = plan.get('stay') == 'home'
            if scene['home']:
                scene['tags'] = list(set(scene.get('tags', [])) | {'home'})
            return await self._workspace_commit_scene(scene, now, engine)
        allowed = self._district_neighbors(district)
        place = self._pick_place(activities, city, allowed, exclude=self._visited_today(),
                                 stay_in_allowed=True)
        if place:
            target_district = str(place.get('district') or district)
            if target_district != district:
                weather = await self._weather(target_district, city)
            scene = self._place_scene(
                place, templates, weather, season,
                (f"{weather.get('desc', '')}，{weather.get('temp')}℃"
                 if weather.get('temp') is not None else '天气暂不可用'),
                float(20 if weather.get('temp') is None else weather['temp']),
                hour, city)
            self._record_visit(str(place.get('name') or ''))
            return await self._workspace_commit_scene(scene, now, engine)
        if city == profile.home_city:
            scene = self._home_scene('home_evening', bed_type, activities, templates, season, weather)
            scene['home'] = True
            scene['tags'] = list(set(scene.get('tags', [])) | {'home'})
            return await self._workspace_commit_scene(scene, now, engine)
        fixed = scene_by_id(None, self.rng)
        scene = self._lodging_scene(city, district, activities, season, weather, fixed=fixed)
        return await self._workspace_commit_scene(scene, now, engine)
    async def _travel_scene(self, now, local, city, district, activities, templates, season):
        """One hour of inter-city travel, or None when nothing travel-related applies.

        Rules kept from the legacy engine: a trip is decided once per day, you must
        spend one hour in a station district before boarding, boarding only happens
        inside the departure window, and arrival is a separate hour.
        """
        trip = self.state.get('trip') if isinstance(self.state.get('trip'), dict) else {}
        to_city = str(trip.get('to') or '')
        if to_city and to_city == city:
            self.state.pop('trip', None)
            trip, to_city = {}, ""
        if not to_city and self._wants_new_trip(city, int(local.tm_hour), now):
            destination = self._trip_destination(city)
            if destination:
                self.state['trip'] = {'to': destination, 'stage': 'wait',
                                      'date': self._world_strftime('%Y-%m-%d'), 'from': city}
                trip, to_city = self.state['trip'], destination
        if not to_city:
            return None
        stations = self._station_districts(city)
        if not stations:
            self.state.pop('trip', None)
            return None
        weather = await self._weather(district, city)
        if district in stations and self._can_board_now(int(local.tm_hour)):
            hours = max(1, int(round(self._travel_hours(city, to_city) or 1)))
            target_district = next(iter(self._station_districts(to_city)), "")
            self.state['pending_target'] = {
                'city': to_city, 'district': target_district, 'kind': 'station',
                'place': f'{to_city}的车站大厅', 'name': f'抵达{to_city}',
                'arrive_at': now + hours * 3600,
            }
            self.state.pop('trip', None)
            return self._transit_scene('train', f"正在前往{to_city}", city, district, season, weather)
        if district in stations:
            return self._station_scene(city, district, season, weather)
        hop = self._next_hop_towards(district, stations, city, activities)
        if not hop:
            self.state.pop('trip', None)
            return None
        place = self._pick_place(activities, city, {hop}, exclude=self._visited_today(),
                                 stay_in_allowed=True)
        if not place:
            return self._station_scene(city, hop, season, weather)
        scene = self._place_scene(place, templates, weather, season,
                                  (f"{weather.get('desc', '')}，{weather.get('temp')}℃"
                                   if weather.get('temp') is not None else '天气暂不可用'),
                                  float(20 if weather.get('temp') is None else weather['temp']),
                                  int(local.tm_hour), city)
        self._record_visit(str(place.get('name') or ''))
        return scene

