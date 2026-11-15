"""Offline, framework-free city validation and simulated world drafting.

The bundled administrative names come from a sourced NBS 2023 snapshot, not
from the LLM. It is not a current authoritative lookup service. Unknown names
fail closed. District coordinates/places/stations are generated scene data:
validation proves structure and administrative membership, NOT real locations,
station existence, geographic containment, or authoritative shared boundaries.
No network calls, model calls, persistence, clock or runtime restart occur here.
"""
from copy import deepcopy
import unicodedata
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path

from .geography import CityCatalog, _coordinate

SIMULATED_ADJACENCY_NOTICE = "生成的模拟邻接，非权威行政区边界；坐标、地点及车站仅供模拟场景，未经实地核验"
SCENIC_PLACES_ERROR = "每个区至少要有1个景点（公园/景区/古迹/博物馆等），且整座城市景点不少于区县数"
LODGING_NOT_ALLOWED = "城市地点不生成过夜点（酒店/青旅/旅馆/网吧），过夜由固定场景处理"
# 景点优先：生成 50 地点时先排真实/合理的景点，再补生活场景。
_SCENIC_WORDS = ("公园", "景区", "景点", "博物馆", "纪念馆", "遗址", "古镇", "老街", "湿地",
                 "动物园", "植物园", "美术馆", "展览馆", "剧院", "步行街", "寺", "庙", "塔",
                 "湖", "山", "不夜城", "古城", "城墙", "石窟", "瀑布", "温泉", "海滩", "岛",
                 "峡谷", "洞", "陵", "宫", "苑", "景观", "地标", "观景", "故居")

# Undirected provincial land-border graph. Water crossings are deliberately
# excluded (Hainan/Taiwan are isolated). Hours are GAME rules, not timetables.
_BORDER_ROWS = {
    "北京": "河北 天津", "天津": "河北", "河北": "辽宁 内蒙古 山西 河南 山东",
    "山西": "内蒙古 陕西 河南", "内蒙古": "黑龙江 吉林 辽宁 陕西 宁夏 甘肃",
    "辽宁": "吉林", "吉林": "黑龙江", "黑龙江": "",
    "上海": "江苏 浙江", "江苏": "山东 安徽 浙江", "浙江": "安徽 江西 福建",
    "安徽": "山东 河南 湖北 江西", "福建": "江西 广东", "江西": "湖北 湖南 广东",
    "山东": "河南", "河南": "陕西 湖北", "湖北": "陕西 重庆 湖南",
    "湖南": "重庆 贵州 广西 广东", "广东": "广西 香港 澳门", "广西": "贵州 云南",
    "海南": "", "重庆": "陕西 四川 贵州", "四川": "陕西 甘肃 青海 西藏 云南 贵州",
    "贵州": "云南", "云南": "西藏", "西藏": "新疆 青海", "陕西": "甘肃 宁夏",
    "甘肃": "宁夏 青海 新疆", "青海": "新疆", "宁夏": "", "新疆": "",
    "台湾": "", "香港": "", "澳门": "",
}
_PROVINCE_ALIASES = {"内蒙古自治区": "内蒙古", "广西壮族自治区": "广西",
                     "西藏自治区": "西藏", "宁夏回族自治区": "宁夏",
                     "新疆维吾尔自治区": "新疆", "香港特别行政区": "香港",
                     "澳门特别行政区": "澳门"}


def _text(value, label):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{label}必须是非空字符串")
    return value.strip()


def _province(value):
    value = _text(value, "省份")
    value = _PROVINCE_ALIASES.get(value, value.removesuffix("省").removesuffix("市"))
    if value not in _BORDER_ROWS:
        raise ValueError(f"未知省级行政区：{value}")
    return value


def province_adjacency():
    """Return a fresh symmetric graph; callers cannot mutate shared topology."""
    graph = {p: set() for p in _BORDER_ROWS}
    for p, row in _BORDER_ROWS.items():
        for q in row.split():
            graph[p].add(q)
            graph[q].add(p)
    return {p: frozenset(qs) for p, qs in graph.items()}


def province_shortest_path(origin, destination):
    origin, destination = _province(origin), _province(destination)
    graph = province_adjacency()
    queue = deque([(origin,)])
    seen = {origin}
    while queue:
        path = queue.popleft()
        if path[-1] == destination:
            return path
        for neighbor in sorted(graph[path[-1]] - seen):
            seen.add(neighbor)
            queue.append((*path, neighbor))
    raise ValueError("省份间无陆地邻接路径，不能虚构跨海路线")


def province_travel_hours(origin, destination):
    """max(1, minimum land-border edges); not real train journey hours."""
    return max(1, len(province_shortest_path(origin, destination)) - 1)


@dataclass(frozen=True)
class CityRecord:
    name: str
    province: str
    code: str
    districts: tuple[str, ...]


@lru_cache(maxsize=1)
def _bundled_records():
    with Path(__file__).with_name("cities_cn.json").open(encoding="utf-8") as stream:
        return _records(json.load(stream))


def _records(registry):
    """Injected registries are a TRUSTED caller boundary, never model output."""
    if not isinstance(registry, Mapping) or not isinstance(registry.get("source"), Mapping):
        raise ValueError("行政区数据缺少可靠来源元数据")
    source = registry["source"]
    if not source.get("url") or not source.get("as_of"):
        raise ValueError("行政区数据缺少来源URL或时间")
    rows = registry.get("cities")
    if not isinstance(rows, list) or not rows:
        raise ValueError("行政区数据不可用，禁止由LLM猜测城市")
    result = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("districts"), list):
            raise ValueError("行政区数据结构无效")
        result.append(CityRecord(_text(row.get("name"), "城市"),
                                 _text(row.get("province"), "省份"),
                                 _text(row.get("code"), "行政区代码"),
                                 tuple(_text(d, "区县") for d in row["districts"])))
    return tuple(result)


def resolve_city(name, province=None, *, registry=None):
    """Resolve exact official name or trailing-市 abbreviation, never fuzzy match.

    Pass province separately to disambiguate same-named cities. County-level
    cities can resolve but cannot be drafted without verified subdivisions.
    """
    name = _text(name, "城市")
    records = _bundled_records() if registry is None else _records(registry)
    matches = [r for r in records if name in (r.name, r.name.removesuffix("市"))]
    if province is not None:
        wanted = _province(province)
        matches = [r for r in matches if _province(r.province) == wanted]
    if not matches:
        raise ValueError("不存在该城市，请重新输入")
    if len(matches) > 1:
        choices = "、".join(sorted({r.province for r in matches}))
        raise ValueError(f"城市名称有歧义，请指定省份：{choices}")
    return matches[0]


def city_travel_hours(origin, destination, origin_province=None, destination_province=None, *, registry=None):
    a = resolve_city(origin, origin_province, registry=registry)
    b = resolve_city(destination, destination_province, registry=registry)
    return province_travel_hours(a.province, b.province)


def validate_min_stay_days(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("min_stay_days必须是至少1的整数")
    return value


def validate_location_count(count):
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("地点数量count必须是1到100的整数")
    return count


def _place_identity(name):
    return "".join(unicodedata.normalize("NFKC", name).split()).casefold()


def validate_city_draft(city, draft, province=None, *, registry=None, count=50):
    """Validate untrusted model JSON and return new JSON-serializable data.

    draft = {districts: {name: {lat, lon, neighbors: [name]}},
             stations: [district], min_stay_days: positive_int,
             locations: [{name, district, kind, place, lat?, lon?}] * 50}
    data is directly accepted by CityCatalog.from_dict. Output city key follows
    the caller's validated spelling (e.g. a short name rather than forcing the full name).
    Unknown model fields are discarded; administrative authority comes ONLY
    from registry, never draft. All configured districts must contain places.
    """
    count = validate_location_count(count)
    record = resolve_city(city, province, registry=registry)
    city = city.strip()
    if len(record.districts) < 2:
        raise ValueError("缺少可靠的多个区县数据，不能生成城市")
    if not isinstance(draft, Mapping):
        raise ValueError("城市草稿必须是对象")
    stay = validate_min_stay_days(draft.get("min_stay_days"))
    raw = draft.get("districts")
    if not isinstance(raw, Mapping) or len(raw) < 2:
        raise ValueError("地点必须分布在多个真实区县，而不是虚构行政区")
    if count < max(len(raw), 5):
        raise ValueError("完整新城地点数量不足：至少覆盖全部配置区县、车站及每区景点；已有城市请使用追加模式")
    districts = {}
    for key, entry in raw.items():
        name = _text(key, "区县")
        if name not in record.districts:
            raise ValueError(f"该城市不存在区县：{name}")
        if name in districts or not isinstance(entry, Mapping):
            raise ValueError("区县重复或定义无效")
        neighbors = entry.get("neighbors")
        if not isinstance(neighbors, list):
            raise ValueError("neighbors必须是区县列表")
        neighbors = [_text(n, "相邻区县") for n in neighbors]
        if name in neighbors or len(set(neighbors)) != len(neighbors):
            raise ValueError("邻接不能自指或重复")
        districts[name] = dict(lat=_coordinate(entry.get("lat"), 90, "lat"),
                               lon=_coordinate(entry.get("lon"), 180, "lon"), neighbors=neighbors)
    stations = draft.get("stations")
    if not isinstance(stations, list) or not stations:
        raise ValueError("至少需要一个车站所在区县")
    stations = [_text(s, "车站区县") for s in stations]
    if len(stations) != len(set(stations)):
        raise ValueError("车站区县不能重复")
    data = {city: {"districts": districts, "stations": stations}}
    CityCatalog.from_dict(data)  # coordinate and dangling reference validation
    for name, entry in districts.items():
        for neighbor in entry["neighbors"]:
            if name not in districts[neighbor]["neighbors"]:
                raise ValueError("模拟邻接必须双向")
    seen, queue = set(), [next(iter(districts))]
    while queue:
        name = queue.pop()
        if name not in seen:
            seen.add(name)
            queue.extend(districts[name]["neighbors"])
    if len(seen) != len(districts):
        raise ValueError("模拟区县邻接图必须连通")
    places = draft.get("locations")
    if not isinstance(places, list) or len(places) != count:
        raise ValueError(f"必须生成恰好{count}个唯一地点")
    activities, names, used = [], set(), set()
    for place in places:
        if not isinstance(place, Mapping):
            raise ValueError("地点必须是对象")
        name = _text(place.get("name"), "地点名称")
        identity = _place_identity(name)
        if identity in names:
            raise ValueError(f"必须生成恰好{count}个唯一地点")
        names.add(identity)
        district = _text(place.get("district"), "地点区县")
        if district not in districts:
            raise ValueError(f"地点引用未知区县：{district}")
        used.add(district)
        kind = place.get("kind")
        if kind == "lodging":
            raise ValueError(LODGING_NOT_ALLOWED)
        if kind not in ("indoor", "outdoor", "school", "station"):
            raise ValueError("地点kind必须是indoor/outdoor/school/station，不接受事件或过夜地点")
        if kind == "station" and district not in stations:
            raise ValueError("station地点必须属于stations配置区县")
        activity = dict(name=name, district=district, city=city, kind=kind,
                        place=_text(place.get("place"), "地点环境描述"))
        if "lat" in place or "lon" in place:
            activity.update(lat=_coordinate(place.get("lat"), 90, "地点lat"),
                            lon=_coordinate(place.get("lon"), 180, "地点lon"))
        activities.append(activity)
    if {a["district"] for a in activities if a["kind"] == "station"} != set(stations):
        raise ValueError("每个stations区县至少需要一个station地点")
    if used != set(districts):
        raise ValueError("地点必须覆盖所有配置区县，且分布于多个实际区县")
    scenic = [a for a in activities
              if any(word in str(a.get("name") or "") for word in _SCENIC_WORDS)]
    missing_scenic = [district for district in districts
                      if not any(a["district"] == district for a in scenic)]
    if missing_scenic or len(scenic) < len(districts):
        raise ValueError(SCENIC_PLACES_ERROR)
    # 过夜不再由城市地点承担：固定十间过夜房由引擎在 18 点抽取并复用。
    return dict(data=data, activities=activities, min_stay_days=stay,
                province=record.province, city=city,
                metadata={"adjacency_kind": "generated_simulation",
                          "notice": SIMULATED_ADJACENCY_NOTICE,
                          "administrative_code": record.code})


def city_draft_prompt(city, province=None, *, registry=None, count=50):
    """Build a provider-independent prompt after offline city validation."""
    count = validate_location_count(count)
    if count < 5:
        raise ValueError("完整新城至少需要5个地点；小批补充请使用追加模式")
    record = resolve_city(city, province, registry=registry)
    if len(record.districts) < 2:
        raise ValueError("缺少可靠的多个区县数据，不能生成城市")
    return (f"为{record.province}{record.name}生成模拟生活场景，只返回JSON对象。"
            f"区县只可选：{json.dumps(record.districts, ensure_ascii=False)}。"
            '结构：{"districts":{"实际区名":{"lat":数字,"lon":数字,"neighbors":["区名"]}},'
            '"stations":["车站所在区名"],"min_stay_days":正整数,'
            '"locations":[{"name":"唯一地点名称","district":"实际区名","kind":"outdoor","place":"环境描述"}]}。'
            "kind仅限indoor/outdoor/school/station，不含事件地点，也不要生成酒店/青旅/旅馆/民宿/网吧等过夜地点；"
            "每个stations区县至少一个station地点。"
            f"locations恰好{count}个唯一地点，分布至少两个真实区县，并覆盖所有配置区县。"
            "生成顺序和内容优先级：先为每个配置区县生成至少1个当地知名或合理的景点"
            "（公园、景区、古迹、博物馆、湖山寺庙、老街、湿地等，名称要像真实地名），再补充日常生活场景；"
            "景点总数不少于区县数。生活场景（商场、超市、小学、中学、菜场、餐饮等）不要占太多名额，"
            "不要每个区都重复商场和小学。"
            "districts不是50个虚构区；邻接双向、无悬空、无自环且连通；坐标必须有限且经纬度范围有效。"
            + SIMULATED_ADJACENCY_NOTICE)


def _append_context(city, existing, province, registry):
    record = resolve_city(city, province, registry=registry)
    city = city.strip()
    if not isinstance(existing, Mapping) or not isinstance(existing.get("data"), Mapping):
        raise ValueError("追加需要已有城市data；无有效城市数据请完整生成")
    data = existing["data"]
    definition = data.get(city)
    if not isinstance(definition, Mapping) or not definition.get("districts"):
        raise ValueError("追加需要已有城市有效区县数据；空占位请完整生成")
    catalog = CityCatalog.from_dict({city: definition})
    districts = catalog.district_names(city)
    if any(d not in record.districts for d in districts):
        raise ValueError("已有城市含不属于该城市的区县，不能追加")
    # Do not force old cities to satisfy newer scenic/night quotas. Preserve old
    # topology and places exactly; only new entries are subject to new rules.
    activities = existing.get("activities")
    if not isinstance(activities, list) or any(not isinstance(a, Mapping) for a in activities):
        raise ValueError("已有activities必须是地点对象列表")
    for a in activities:
        _text(a.get("name"), "已有地点名称")
        if a.get("city", city) != city or a.get("district") not in districts:
            raise ValueError("已有地点城市或区县与城市数据不一致")
    return city, record, catalog, activities


def city_append_prompt(city, existing, count=30, province=None, *, registry=None):
    """Prompt only a new batch; old topology and places are immutable context."""
    count = validate_location_count(count)
    city, record, catalog, old = _append_context(city, existing, province, registry)
    return (f"为{record.province}{record.name}追加恰好{count}个唯一地点，只返回JSON。"
            '结构：{"locations":[{"name":"具体地点名称","district":"已有区县",'
            '"kind":"outdoor","place":"具体环境描述"}]}。'
            "不返回或修改districts/stations/min_stay_days，不重建城市，不重复旧地点。"
            f"已有区县：{json.dumps(catalog.district_names(city), ensure_ascii=False)}；"
            f"已有车站区县：{json.dumps(catalog.station_districts(city), ensure_ascii=False)}。"
            f"禁止重复的已有名称：{json.dumps([a['name'] for a in old], ensure_ascii=False)}。"
            "本批不要生成酒店/青旅/旅馆/民宿/网吧等过夜地点，过夜由固定场景处理。"
            "剩余名额优先当地景点，再补生活地点；追加可集中已有某一区，不要求每区新增景点或覆盖全区。"
            "kind仅限indoor/outdoor/school/station，station只能在已有车站区县；不要事件专属地点。"
            "已有名称按全角半角、空白、大小写归一后均不可重复，同名换区也不允许。"
            + SIMULATED_ADJACENCY_NOTICE)


def validate_city_append(city, draft, existing, count=30, province=None, *, registry=None):
    """Return a detached full merged result, without mutating or replacing old data.

    Count 1..100 denotes NEW places, not resulting total. New batches never add
    overnight rooms; existing scenic deficits never reject a valid small append.
    """
    count = validate_location_count(count)
    city, record, catalog, old = _append_context(city, existing, province, registry)
    if not isinstance(draft, Mapping):
        raise ValueError("追加草稿必须是对象")
    for field in ("data", "districts", "stations", "min_stay_days"):
        if field in draft:
            raise ValueError("追加只允许locations，不得替换已有区县、车站或停留设置")
    places = draft.get("locations")
    if not isinstance(places, list) or len(places) != count:
        raise ValueError(f"本次追加必须恰好{count}个唯一新地点")
    names = {_place_identity(a["name"]) for a in old}
    additions = []
    for place in places:
        if not isinstance(place, Mapping):
            raise ValueError("地点必须是对象")
        name = _text(place.get("name"), "地点名称")
        identity = _place_identity(name)
        if identity in names:
            raise ValueError(f"追加地点与已有地点或本批地点重复：{name}")
        names.add(identity)
        district = _text(place.get("district"), "地点区县")
        if district not in catalog.district_names(city):
            raise ValueError(f"追加地点引用未知区县：{district}")
        kind = place.get("kind")
        if kind == "lodging":
            raise ValueError(LODGING_NOT_ALLOWED)
        if kind not in ("indoor", "outdoor", "school", "station"):
            raise ValueError("地点kind无效，不接受事件或过夜地点")
        if place.get("event_only") or place.get("event_id"):
            raise ValueError("追加不接受事件专属地点")
        if kind == "station" and district not in catalog.station_districts(city):
            raise ValueError("station地点必须属于已有stations配置区县")
        entry = dict(name=name, district=district, city=city, kind=kind,
                     place=_text(place.get("place"), "地点环境描述"))
        if "lat" in place or "lon" in place:
            entry.update(lat=_coordinate(place.get("lat"), 90, "地点lat"),
                         lon=_coordinate(place.get("lon"), 180, "地点lon"))
        additions.append(entry)
    result = deepcopy(dict(existing))
    result["activities"] = deepcopy(old) + additions
    metadata = dict(result.get("metadata") or {})
    metadata.update(added_count=count, total_count=len(result["activities"]))
    result["metadata"] = metadata
    return result
