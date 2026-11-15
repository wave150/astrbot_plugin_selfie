"""Pure, injected-clock living rules. No network, filesystem, framework or eval.

Call ``scene(datetime, ...)`` before ordinary world planning; a non-None scene
owns this tick (including event preparation). Persist the caller's state normally.
Only ``state['living']`` is mutated. All persisted values are JSON compatible.
Events use explicit conditions, start HH:MM, duration in hours, and phase
start/end hour offsets. Weekdays use Python's Monday=0 convention. The caller
supplies local wall-clock datetime consistently, and city-specific adjacency.
"""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import math
import re
from .overnight import pick_overnight

OUTFIT_MODES = ('fixed', 'event', 'inherit')


MEALS = (
    '番茄炒蛋', '宫保鸡丁', '鱼香肉丝', '青椒肉丝', '土豆炖牛肉',
    '红烧肉', '糖醋里脊', '可乐鸡翅', '香菇滑鸡', '清蒸鲈鱼',
    '蒜蓉西兰花', '清炒油麦菜', '手撕包菜', '醋溜土豆丝', '麻婆豆腐',
    '家常豆腐', '西葫芦炒蛋', '韭菜炒蛋', '虾仁炒蛋', '木耳炒肉',
    '冬瓜排骨汤', '玉米排骨汤', '紫菜蛋花汤', '番茄牛腩', '咖喱鸡肉饭',
    '扬州炒饭', '蛋炒饭', '番茄鸡蛋面', '葱油拌面', '牛肉面',
    '炸酱面', '鸡丝凉面', '香菇鸡肉粥', '皮蛋瘦肉粥', '蔬菜粥',
    '鲜肉水饺', '韭菜鸡蛋饺子', '鲜虾馄饨', '鸡蛋三明治', '金枪鱼三明治',
    '照烧鸡腿饭', '香煎三文鱼', '肉末蒸蛋', '清蒸南瓜', '香菇青菜',
    '蒜苔炒肉', '芹菜炒香干', '山药炒木耳', '白菜炖粉条', '豆角焖面',
    '汉堡套餐', '炸鸡配可乐', '披萨',
)

# 在家吃饭时，把「厨房里的小餐桌」补进场景描述：家里的厨房本来没写餐桌。
_MEAL_TABLE_HINT = '；厨房靠窗的位置摆着一张原木小餐桌，桌上已经摆好刚做好的饭菜和碗筷'
_CONDITIONS = {'weekdays', 'cities', 'dates', 'date_from', 'date_to'}


def _hours(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('hour offsets must be finite numbers')
    return float(value)


def _validate_outfit_pool(rows, label):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'{label} must be a non-empty list')
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'].strip()
                or not isinstance(row.get('desc'), str) or not row['desc'].strip()):
            raise ValueError(f'{label} entries need name and desc')


def _pool_entries(part, key, resolved_key):
    """Prefer resolved ``*_items``; fall back to hand-written inline pools."""
    items = part.get(resolved_key)
    if isinstance(items, list) and items:
        return items
    rows = part.get(key)
    return rows if isinstance(rows, list) else []


def _validate_pools(part, *, is_phase, phase_index):
    """Validate optional random pools: raw ref arrays and resolved ``*_items``."""
    if 'scene_pool' in part:
        rows = part['scene_pool']
        if not isinstance(rows, list) or not rows:
            raise ValueError('scene_pool must be a non-empty list')
        for row in rows:
            if isinstance(row, str) and row.strip():
                continue
            if isinstance(row, dict) and str(row.get('text') or '').strip():
                continue
            raise ValueError('scene_pool entries must name an event scene')
    if 'action_pool' in part:
        rows = part['action_pool']
        if not isinstance(rows, list) or not rows:
            raise ValueError('action_pool must be a non-empty list')
        for row in rows:
            if isinstance(row, str) and row.strip():
                continue
            if isinstance(row, dict) and row.get('source') in ('event', 'global') \
                    and isinstance(row.get('id'), (str, int)):
                continue
            raise ValueError('action_pool entries must be scoped references or text')
    if 'outfit_pool' in part:
        rows = part['outfit_pool']
        if not isinstance(rows, list) or not rows:
            raise ValueError('outfit_pool must be a non-empty list')
        for row in rows:
            if isinstance(row, dict) and row.get('source') in ('event', 'global') \
                    and isinstance(row.get('id'), (str, int)):
                continue
            if isinstance(row, dict) and str(row.get('name') or '').strip() \
                    and str(row.get('desc') or '').strip():
                continue
            raise ValueError('outfit_pool entries must be scoped references')
    if 'scene_pool_items' in part:
        rows = part['scene_pool_items']
        if not isinstance(rows, list) or not rows:
            raise ValueError('scene_pool must be a non-empty list')
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('text'), str)
                    or not row['text'].strip()):
                raise ValueError('scene_pool entries need text')
    if 'action_pool_items' in part:
        rows = part['action_pool_items']
        if not isinstance(rows, list) or not rows:
            raise ValueError('action_pool must be a non-empty list')
        if any(not isinstance(text, str) or not text.strip() for text in rows):
            raise ValueError('action_pool entries must be non-empty text')
    if 'outfit_pool_items' in part:
        _validate_outfit_pool(part['outfit_pool_items'], 'outfit_pool')
    mode = part.get('outfit_mode')
    if mode is not None:
        if mode not in OUTFIT_MODES:
            raise ValueError('invalid outfit_mode')
        if not is_phase:
            raise ValueError('outfit_mode is phase-only')
        if mode == 'inherit':
            source = part.get('outfit_inherit')
            if type(source) is not int or not 0 <= source < phase_index:
                raise ValueError('outfit_inherit must point to an earlier phase')


def validate_event(event):
    """Return a detached validated definition; reject unsafe/ambiguous conditions."""
    e = deepcopy(event)
    if not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e[k].strip() for k in ('id', 'city', 'district')):
        raise ValueError('event needs id, city and district')
    if 'end_scene_enabled' in e and type(e['end_scene_enabled']) is not bool:
        raise ValueError('end_scene_enabled must be boolean')
    c = e.get('conditions')
    if not isinstance(c, dict) or set(c) - _CONDITIONS:
        raise ValueError('explicit whitelisted conditions required')
    for key in ('cities', 'dates', 'weekdays'):
        if key in c and not isinstance(c[key], list):
            raise ValueError('condition values must be lists')
    if any(type(n) is not int or not 0 <= n <= 6 for n in c.get('weekdays', [])):
        raise ValueError('weekdays must be 0..6')
    if any(not isinstance(s, str) or not s for s in c.get('cities', [])):
        raise ValueError('cities must be names')
    for value in c.get('dates', []) + [c[k] for k in ('date_from', 'date_to') if k in c]:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError('dates must use ISO format')
        date.fromisoformat(value)
    if c.get('date_from', '') > c.get('date_to', '9999-12-31'):
        raise ValueError('inverted date range')
    if not isinstance(e.get('start'), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', e['start']):
        raise ValueError('start must be HH:MM')
    if not 0 < _hours(e.get('duration')) <= 24 * 366:
        raise ValueError('duration must be positive and at most one year')
    if not 0 <= _hours(e.get('probability', 1)) <= 1:
        raise ValueError('probability must be 0..1')
    phases = e.get('phases', [])
    if not isinstance(phases, list):
        raise ValueError('phases must be a list')
    previous = 0
    for p in phases:
        if not isinstance(p, dict):
            raise ValueError('phase must be an object')
        start, end = _hours(p.get('start')), _hours(p.get('end'))
        if not previous <= start < end <= e['duration']:
            raise ValueError('phases must be ordered, nonoverlapping and within duration')
        previous = end
    for index, part in enumerate([e] + phases):
        if 'scene' in part and not isinstance(part['scene'], (str, dict)):
            raise ValueError('scene must be text or object')
        if 'outfit' in part and not isinstance(part['outfit'], (str, dict)):
            raise ValueError('outfit must be text or object')
        if 'action' in part and not isinstance(part['action'], str):
            raise ValueError('action must be text')
        _validate_pools(part, is_phase=index > 0, phase_index=index - 1)
    if 'end_scene' in e and not isinstance(e['end_scene'], (str, dict)):
        raise ValueError('end_scene must be text or object')
    for raw in [e.get('scene'), e.get('end_scene')] + [p.get('scene') for p in phases]:
        if isinstance(raw, dict) and any(k in raw and raw[k] != e[k] for k in ('city', 'district')):
            raise ValueError('event scenes must remain in the event city and district')
    return e


def _matches(c, day, city):
    key = day.isoformat()
    return (('weekdays' not in c or day.weekday() in c['weekdays'])
            and ('cities' not in c or city in c['cities'])
            and ('dates' not in c or key in c['dates'])
            and c.get('date_from', key) <= key <= c.get('date_to', key))


def photo_variation(event, phase_index, rng):
    """Pick one scene entry and one action text for a single photo inside an event.

    Event phases may carry ``scene_pool`` / ``action_pool`` (already resolved to
    inline values by ``event_materials.resolve_event``). The first selected
    material stays the primary for schedule/diary; pools only add per-photo
    variety. Returns ``(scene_entry | None, action_text)``.
    """
    phases = event.get('phases') if isinstance(event, dict) else None
    if not isinstance(phases, list):
        phases = []
    if phase_index == -2:
        index = len(phases) - 1
    elif type(phase_index) is int:
        index = phase_index
    else:
        index = 0
    part = phases[index] if 0 <= index < len(phases) else {}
    scenes = [row for row in _pool_entries(part, 'scene_pool', 'scene_pool_items')
              if isinstance(row, dict) and str(row.get('text') or '').strip()]
    actions = [text.strip() for text in _pool_entries(part, 'action_pool', 'action_pool_items')
               if isinstance(text, str) and text.strip()]
    scene = deepcopy(rng.choice(scenes)) if scenes else None
    action = str(rng.choice(actions)) if actions else ''
    return scene, action


class LivingEngine:
    def __init__(self, state, rng):
        self.state = state.setdefault('living', {})
        self.rng = rng
        for key in ('days', 'records', 'diaries'):
            self.state.setdefault(key, {})

    def record(self, now, scene):
        """Record only a scene actually emitted/observed, once per hour+content."""
        rows = self.state['records'].setdefault(now.date().isoformat(), [])
        row = {k: deepcopy(scene[k]) for k in ('city', 'district', 'activity', 'type', 'event_id', 'living_phase', 'meal', 'room') if k in scene}
        row['at'] = now.isoformat()
        fingerprint = dict(row, at=now.replace(minute=0, second=0, microsecond=0).isoformat())
        for old in rows:
            other = dict(old, at=datetime.fromisoformat(old['at']).replace(minute=0, second=0, microsecond=0).isoformat())
            if other == fingerprint:
                return
        rows.append(row)

    def schedule(self, day, now=None):
        """Read-only timetable: planned intervals and genuine observation points.

        Observation end equals start: polling proves presence at that instant,
        not continuous activity until the next poll. No future observation enters
        the diary, and reading this API never rolls or accepts a new plan.
        """
        day = date.fromisoformat(day) if isinstance(day, str) else day
        lower = datetime.combine(day, time.min)
        upper = lower + timedelta(days=1)
        rows = []
        def add(start, end, activity, **fields):
            start, end = max(start, lower), min(end, upper)
            if start < end:
                rows.append(dict(start=start.isoformat(), end=end.isoformat(), activity=activity,
                                 status='planned', **fields))
        daily = self.state.get('days', {}).get(day.isoformat(), {})
        lunch = daily.get('lunch', {})
        if daily.get('stay_home'):
            add(lower.replace(hour=7), lower.replace(hour=11), '在家休息')
            add(lower.replace(hour=11), lower.replace(hour=12), '准备午餐：' + lunch.get('meal', ''))
            add(lower.replace(hour=13), lower.replace(hour=14), '在家休息')
        if lunch:
            add(lower.replace(hour=12), lower.replace(hour=13), '午餐：' + lunch.get('meal', ''), meal=lunch.get('meal'))
        plans = list(self.state.get('event_plans', {}).values())
        if self.state.get('active') and not any(p.get('start') == self.state['active'].get('start') and p['event']['id'] == self.state['active']['event']['id'] for p in plans):
            plans.append(self.state['active'])
        for plan in plans:
            event = plan['event']
            start, end = datetime.fromisoformat(plan['start']), datetime.fromisoformat(plan['end'])
            common = dict(event_id=event['id'], city=event['city'], district=event['district'])
            add(start - timedelta(hours=1), start, '提前准备：' + event.get('name', event['id']), **common)
            for index, phase in enumerate(event.get('phases') or [{'start': 0, 'end': event['duration']}]):
                add(start + timedelta(hours=phase['start']), start + timedelta(hours=phase['end']),
                    phase.get('action') or event.get('action') or event.get('name', event['id']), phase_index=index, **common)
            if event.get('end_scene_enabled', True):
                add(end, end + timedelta(hours=1), event.get('name', event['id']) + '结束后场景', phase_index=-2, **common)
        # Accepted event intervals take precedence over tentative home/lunch.
        event_rows = [row for row in rows if row.get('event_id')]
        clipped = list(event_rows)
        for row in rows:
            if row.get('event_id'):
                row['source'] = 'accepted_event_snapshot'
                continue
            pieces = [row]
            for occupied in event_rows:
                remaining = []
                for piece in pieces:
                    if piece['end'] <= occupied['start'] or piece['start'] >= occupied['end']:
                        remaining.append(piece)
                    else:
                        if piece['start'] < occupied['start']:
                            remaining.append(dict(piece, end=occupied['start']))
                        if piece['end'] > occupied['end']:
                            remaining.append(dict(piece, start=occupied['end']))
                pieces = remaining
            clipped.extend(pieces)
        rows = clipped
        cursor = lower
        for planned in sorted(rows, key=lambda row: row['start']):
            begin, finish = datetime.fromisoformat(planned['start']), datetime.fromisoformat(planned['end'])
            if begin > cursor:
                rows.append(dict(start=cursor.isoformat(), end=begin.isoformat(), activity='未安排', status='unplanned'))
            cursor = max(cursor, finish)
        if cursor < upper:
            rows.append(dict(start=cursor.isoformat(), end=upper.isoformat(), activity='未安排', status='unplanned'))
        for record in self.state.get('records', {}).get(day.isoformat(), []):
            at = datetime.fromisoformat(record['at'])
            if now is not None and at > now:
                continue
            rows.append(dict(deepcopy(record), start=record['at'], end=record['at'], status='observed'))
        return sorted(rows, key=lambda row: (row['start'], row['status']))

    def diary(self, day):
        """Freeze one diary per date using only observations already recorded."""
        key = day.isoformat() if isinstance(day, date) else str(day)
        if key not in self.state['diaries']:
            rows = [row for row in self.schedule(key) if row['status'] == 'observed']
            if not rows:
                return ''
            phrases = []
            for row in rows:
                text = f"{row.get('city', '')}{row.get('district', '')}：{row.get('activity', '记录日常')}"
                if text not in phrases:
                    phrases.append(text)
            self.state['diaries'][key] = '；'.join(phrases) + '。'
        return self.state['diaries'][key]

    def _emit(self, now, scene, weather):
        if scene.get('home'):
            room = getattr(self, 'home_rooms', {}).get(scene.get('room'))
            if isinstance(room, str) and room:
                scene['desc'] = room
            elif isinstance(room, dict) and room.get('desc'):
                scene['desc'] = str(room['desc'])
        if scene.get('meal_table') and isinstance(scene.get('desc'), str) and scene['desc'].strip():
            desc = scene['desc'].rstrip()
            scene['desc'] = (desc[:-1] if desc.endswith('。') else desc) + _MEAL_TABLE_HINT + '。'
        scene['can_change_clothes'] = bool(scene.get('home') or scene.get('type') == 'lodging')
        scene['weather'] = deepcopy(weather or {})
        self.record(now, scene)
        return scene

    @staticmethod
    def _home(city, district, room, action, phase):
        return {'type': 'home_evening', 'home': True, 'tags': ['home'], 'room': room,
                'city': city, 'district': district, 'desc': f'家里的{room}',
                'activity': action, 'living_phase': phase}

    def _phase_outfit(self, active, event, index, seen=()):
        """Resolve the outfit worn during one phase.

        只有三种来源：``fixed``（本段固定一套）、``event``（事件随机池，整场摇一次）、
        ``inherit``（沿用前面某个阶段实际穿的那套）。子事件内部没有自己的随机池，
        保证同一场子事件里衣服始终一致。
        """
        phases = event.get('phases') or []
        if type(index) is not int or not 0 <= index < len(phases):
            return None
        if index in seen:
            return None
        part = dict(event, **phases[index])
        mode = str(part.get('outfit_mode') or '')
        if mode not in OUTFIT_MODES:
            mode = 'event' if event.get('outfit_pool') else 'fixed'
        if mode == 'fixed':
            outfit = part.get('outfit') or event.get('outfit')
            return deepcopy(outfit) if outfit else None
        if mode == 'event':
            rolled = active.get('rolled_outfit')
            if not rolled:
                pool = [row for row in _pool_entries(event, 'outfit_pool', 'outfit_pool_items')
                        if isinstance(row, dict) and str(row.get('name') or '').strip()]
                if pool:
                    rolled = deepcopy(self.rng.choice(pool))
                elif event.get('outfit') or part.get('outfit'):
                    rolled = deepcopy(event.get('outfit') or part.get('outfit'))
                active['rolled_outfit'] = deepcopy(rolled) if rolled else None
            return deepcopy(rolled) if rolled else None
        if mode == 'inherit':
            source = part.get('outfit_inherit')
            if type(source) is not int:
                return None
            return self._phase_outfit(active, event, source, seen + (index,))
        return None

    def _event_scene(self, active, now):
        e = active['event']
        start, end = datetime.fromisoformat(active['start']), datetime.fromisoformat(active['end'])
        phase = 'prepare' if now < start else 'end' if now >= end else 'active'
        # New editor schema has phase-only materials; preparation uses the
        # first phase rather than reviving hidden obsolete top-level defaults.
        part = dict(e, **e['phases'][0]) if e.get('phases') else e
        phase_index = None
        if phase == 'active':
            offset = (now - start).total_seconds() / 3600
            for i, p in enumerate(e.get('phases', [])):
                if p['start'] <= offset < p['end']:
                    part = dict(e, **p)
                    phase_index = i
                    break
        raw = e.get('end_scene', '活动结束，在附近休息') if phase == 'end' else (
            part.get('scene') or e.get('scene') or e.get('name', e['id']))
        scene = deepcopy(raw) if isinstance(raw, dict) else {'desc': raw}
        scene.setdefault('type', 'indoor')
        # 阶段没写动作/服装时沿用事件主场景的全局值（旧数据里的空字符串同理）。
        scene.setdefault('activity', part.get('action') or e.get('action') or e.get('name') or e['id'])
        if phase == 'prepare':
            scene['activity'] = f"提前到场，准备参加{e.get('name', e['id'])}"
        elif phase == 'end':
            scene['activity'] = f"{e.get('name', e['id'])}结束，在附近休息"
        # 服装：事件随机池整天只摇一次；阶段随机池每段摇一次；也可沿用之前的阶段。
        phases = e.get('phases') or []
        outfit_index = phase_index if phase_index is not None else (
            len(phases) - 1 if phase == 'end' and phases else (0 if phases else None))
        outfit = self._phase_outfit(active, e, outfit_index)
        if outfit is None:
            outfit = part.get('outfit') or e.get('outfit')
        if outfit:
            scene['outfit'] = deepcopy(outfit)
        scene.update(city=e['city'], district=e['district'], event_id=e['id'], living_phase=phase,
                     phase_index=-2 if phase == 'end' else phase_index, home=False)
        return scene

    def scene(self, now, *, city, district, home_city, home_district,
              neighbors=(), home_neighbors=(), events=(), traveling=False, weather=None,
              stay_home_probability=.5, home_rooms=None):
        """Return exclusive scene or None. `traveling` includes planned travel.

        Invalid event definitions fail closed (skipped). New events cannot be
        accepted late, from another city, during travel, or beyond one adjacent
        district. Existing accepted events own the schedule until they end.
        """
        self.home_rooms = home_rooms or {}
        if not 0 <= _hours(stay_home_probability) <= 1:
            raise ValueError('stay_home_probability must be 0..1')
        active = self.state.get('active')
        if active:
            end = datetime.fromisoformat(active['end'])
            if now < end:
                return self._emit(now, self._event_scene(active, now), weather)
            # Optional aftermath owns exactly the real hour after event end,
            # never an hour beginning at a late observation/restart.
            if active['event'].get('end_scene_enabled', True) and now < end + timedelta(hours=1):
                return self._emit(now, self._event_scene(active, now), weather)
            morning = end.replace(hour=7, minute=0, second=0, microsecond=0)
            if morning <= end:
                morning += timedelta(days=1)
            if (now.hour >= 18 or now.hour < 7) and now < morning:
                e = active['event']
                near = e['city'] == home_city and e['district'] in {home_district, *home_neighbors}
                if near:
                    scene = self._home(home_city, home_district, '卧室' if now.hour < 7 or now.hour >= 22 else '客厅', '活动后回家休息', 'return_home')
                else:
                    room = pick_overnight(self.rng)
                    scene = {'type': 'lodging', 'home': False, 'tags': ['lodging'],
                             'city': e['city'], 'district': e['district'],
                             'desc': room['desc'], 'activity': '活动后就近住宿：' + room['name'],
                             'living_phase': 'lodging', 'overnight_id': room['id']}
                return self._emit(now, scene, weather)
            self.state.pop('active', None)

        # Include tomorrow so 00:xx starts can be prepared on the previous date.
        for candidate in events:
            if isinstance(candidate, dict) and (candidate.get('enabled') is False or candidate.get('material_error')):
                continue
            try:
                e = validate_event(candidate)
            except (ValueError, TypeError, OverflowError):
                continue
            for day in (now.date(), now.date() + timedelta(days=1)):
                start = datetime.combine(day, time.fromisoformat(e['start']), tzinfo=now.tzinfo)
                if not start - timedelta(hours=1) <= now < start:
                    continue
                daily = self.state['days'].setdefault(day.isoformat(), {})
                decisions = daily.setdefault('events', {})
                if e['id'] in decisions:
                    continue
                feasible = (not traveling and city == e['city'] and e['district'] in {district, *neighbors}
                            and _matches(e['conditions'], day, city))
                accepted = feasible and self.rng.random() < e.get('probability', 1)
                decisions[e['id']] = {'accepted': accepted, 'at': now.isoformat(), 'feasible': feasible}
                if accepted:
                    active = {'event': e, 'start': start.isoformat(), 'end': (start + timedelta(hours=e['duration'])).isoformat()}
                    self.state['active'] = active
                    self.state.setdefault('event_plans', {})[e['id'] + ':' + start.isoformat()] = deepcopy(active)
                    return self._emit(now, self._event_scene(active, now), weather)

        if traveling:
            return None
        daily = self.state['days'].setdefault(now.date().isoformat(), {})
        if 'stay_home' not in daily:
            daily['stay_home'] = city == home_city and self.rng.random() < stay_home_probability
            meal = self.rng.choice(MEALS)
            daily['lunch'] = {'meal': meal, 'mode': 'cook' if daily['stay_home'] else 'eat_out',
                              'prepare': '11:00-12:00' if daily['stay_home'] else None, 'eat': '12:00-13:00'}
        # Never claim to have cooked lunch if the first observation is after it.
        if daily['stay_home'] and city == home_city and 7 <= now.hour < 14 and district in {home_district, *home_neighbors}:
            meal = daily['lunch']['meal']
            if now.hour == 11:
                room, action, phase = '厨房', f'准备午餐，做{meal}', 'cook'
            elif now.hour == 12:
                room, action, phase = '厨房', f'在厨房餐桌吃午餐：{meal}', 'lunch'
            else:
                room, action, phase = '客厅', '在家休息，下午两点后再出门', 'home'
            scene = self._home(home_city, home_district, room, action, phase)
            if phase in ('cook', 'lunch'):
                scene['meal'] = meal
            if phase == 'lunch':
                # 吃自己做的饭：就坐在自家厨房的小餐桌前，餐桌上摆着刚做好的饭菜。
                scene['meal_table'] = True
            return self._emit(now, scene, weather)
        if now.hour == 12:
            # Eating nearby is an actual observation, not a retroactive claim.
            # Never attach a named restaurant from another district or city.
            meal = daily['lunch']['meal']
            daily['lunch']['mode'] = 'eat_out'
            daily['lunch']['prepare'] = None
            scene = {'type': 'indoor', 'home': False, 'city': city, 'district': district,
                     'desc': '当前片区附近小餐馆的餐桌旁', 'activity': f'就近吃午餐：{meal}',
                     'living_phase': 'lunch_out', 'meal': meal}
            return self._emit(now, scene, weather)
        return None
