"""Pure event-local named libraries; global references are explicitly scoped.

v1.5.12 起事件阶段支持三类随机池（场景 / 动作 / 服装）：阶段引用仍然保留
``scene_ref`` / ``action_ref`` / ``outfit_ref`` 作为「主素材」，随机池是在主素材
之外额外可选的数组。随机池只在运行期解析成内联值，不改写可编辑的事件文件。
"""
from copy import deepcopy
import hashlib
import json

FIELDS = {'scene': 'scenes', 'end_scene': 'scenes', 'outfit': 'outfits', 'action': 'actions'}
OUTFIT_MODES = ('fixed', 'event', 'inherit')


def _check_shape(event):
    if not isinstance(event, dict):
        raise ValueError('event must be an object')
    phases = event.get('phases', [])
    if not isinstance(phases, list) or any(not isinstance(p, dict) for p in phases):
        raise ValueError('phases must be objects in a list')
    libraries = event.get('materials', {})
    if not isinstance(libraries, dict):
        raise ValueError('materials must be an object')
    for kind in ('scenes', 'outfits', 'actions'):
        rows = libraries.get(kind, [])
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('material libraries must contain objects')


def migrate_event(event):
    """Detach legacy inline values into stable named local entries, idempotently."""
    _check_shape(event)
    e = deepcopy(event)
    libraries = e.setdefault('materials', {})
    if not isinstance(libraries, dict):
        raise ValueError('event materials must be an object')
    for kind in ('scenes', 'outfits', 'actions'):
        libraries.setdefault(kind, [])
    for index, part in enumerate([e] + e.get('phases', [])):
        for field, kind in FIELDS.items():
            if field + '_ref' in part or not part.get(field):
                continue
            value = part[field]
            if isinstance(value, dict):
                text = value.get('desc') or value.get('description') or value.get('name') or ''
                name = value.get('name') or f'{field} {index + 1}'
            else:
                text = str(value)
                name = text[:24]
            token = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            entry = next((row for row in libraries[kind] if row.get('id') == token), None)
            if entry is None:
                entry = {'id': token, 'name': name, 'text': text}
                # Preserve structured scene type/location during old-format migration.
                if field in ('scene', 'end_scene') and isinstance(value, dict):
                    entry['scene'] = deepcopy(value)
                libraries[kind].append(entry)
            part[field + '_ref'] = token if kind == 'scenes' else {'source': 'event', 'id': token}
    if not e.get('phases') and e.get('duration'):
        e['phases'] = [{'start': 0, 'end': e['duration']}]
    for phase in e.get('phases', []):
        for field in ('scene', 'outfit', 'action'):
            ref = field + '_ref'
            if not phase.get(ref) and not phase.get(field):
                if e.get(ref):
                    phase[ref] = deepcopy(e[ref])
                elif e.get(field):
                    phase[field] = deepcopy(e[field])
    e.setdefault('end_scene_enabled', True)
    return e


def _ref_parts(ref, kind):
    """Return (source, token) for a scoped reference."""
    if kind == 'scenes':
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError('scene reference must name an event-local scene')
        return 'event', ref
    if not isinstance(ref, dict) or ref.get('source') not in ('event', 'global') \
            or not isinstance(ref.get('id'), (str, int)):
        raise ValueError('invalid scoped event reference')
    return ref['source'], str(ref['id'])


def _resolve_scene(ref, indexes):
    source, token = _ref_parts(ref, 'scenes')
    row = indexes['scenes'].get(token)
    if row is None:
        raise ValueError('event material reference not found')
    entry = {'name': row['name'], 'text': row['text']}
    if isinstance(row.get('scene'), dict):
        entry['scene'] = deepcopy(row['scene'])
    return entry


def _resolve_outfit(ref, indexes, global_materials):
    source, token = _ref_parts(ref, 'outfits')
    if source == 'event':
        row = indexes['outfits'].get(token)
        if row is None:
            raise ValueError('event material reference not found')
        return {'name': row['name'], 'desc': row['text']}
    rows = (global_materials or {}).get('costume', [])
    row = next((item for item in rows if str(item.get('id')) == token), None)
    if row is None:
        raise ValueError('global material reference not found')
    desc = row.get('description') or row.get('text') or ''
    if not desc:
        raise ValueError('global material has no content')
    return {'name': row.get('name') or f'服装 {token}', 'desc': desc}


def _resolve_action(ref, indexes, global_materials):
    source, token = _ref_parts(ref, 'actions')
    if source == 'event':
        row = indexes['actions'].get(token)
        if row is None:
            raise ValueError('event material reference not found')
        return row['text']
    rows = (global_materials or {}).get('closeup', [])
    row = next((item for item in rows if str(item.get('id')) == token), None)
    if row is None:
        raise ValueError('global material reference not found')
    text = row.get('text') or row.get('description') or row.get('prompt') or ''
    if not text:
        raise ValueError('global material has no content')
    return text


def summarize_action(text, limit=15):
    """把一段生图动作提示词压成一句人话（日程/照片文案用）。

    动作素材的正文是给画图模型的机位与姿态描述，动辄几百字。日程、照片配文、
    聊天背景这些地方只该出现一句概括，所以取第一个句读之前的部分再截断。
    """
    body = str(text or "").strip()
    for separator in ("。", "；", ";", "，", ",", "\n"):
        head, found, _rest = body.partition(separator)
        if found and head.strip():
            body = head.strip()
            break
    return body.strip("，。；;、 ")[:limit]


# 动作素材的 name 应该是「逛商场」这种短名字；从旧数据迁移过来的条目会把
# 提示词正文截前 24 字当名字，这里识别出来并退回事件名当概括。
_ACTION_NAME_HINTS = ("镜头", "机位", "景别", "拍摄", "画面", "构图", "俯视", "平视",
                      "仰视", "特写", "近景", "中景", "全景")


def usable_action_name(name):
    """判断一个动作 name 是否真的是人话短名字；不是就返回空串。"""
    body = str(name or "").strip()
    if not body or len(body) > 12:
        return ""
    if body.strip("，。；;、：:") != body:
        return ""
    if any(word in body for word in _ACTION_NAME_HINTS):
        return ""
    return body


def usable_action_text(text):
    """动作正文本身就是一句短人话时直接拿来用（手写事件常用「听开场」这种）。"""
    body = str(text or "").strip()
    if not body or len(body) > 15:
        return ""
    if body.strip("，。；;、：:") != body:
        return ""
    if any(word in body for word in _ACTION_NAME_HINTS):
        return ""
    return body


def event_label(event):
    """没有可读动作名时，用事件名当概括（日程/配文只出现这一个短句）。"""
    name = str((event or {}).get("name") or "").strip()
    return name[:15]


def action_name(event, part):
    """阶段动作的短名字：素材库 name → 手写短动作 → 事件名 → 概括。

    这是「日程 / 照片配文 / 聊天背景」唯一的动作概括入口：任何事件、任何阶段
    都不允许把长篇生图提示词原样带出去。
    """
    part = part if isinstance(part, dict) else {}
    ref = part.get("action_ref")
    if isinstance(ref, dict) and ref.get("source") == "event":
        rows = ((event or {}).get("materials") or {}).get("actions") or []
        ident = str(ref.get("id"))
        row = next((item for item in rows
                    if isinstance(item, dict) and str(item.get("id")) == ident), None)
        name = usable_action_name((row or {}).get("name"))
        if name:
            return name
    short = usable_action_text(part.get("action"))
    if short:
        return short
    summary = event_label(event)
    if summary:
        return summary
    return usable_action_text(summarize_action(part.get("action"))) or "活动现场"


def action_name_for_text(event, text):
    """按动作正文反查素材库里的短名字（照片随机池抽到的那条）。"""
    body = str(text or "").strip()
    rows = ((event or {}).get("materials") or {}).get("actions") or []
    if body:
        row = next((item for item in rows if isinstance(item, dict)
                    and str(item.get("text") or "").strip() == body), None)
        name = usable_action_name((row or {}).get("name"))
        if name:
            return name
    short = usable_action_text(body)
    if short:
        return short
    summary = event_label(event)
    if summary:
        return summary
    return usable_action_text(summarize_action(body)) or "活动现场"


def _refs(value):
    """Normalize a pool field to a list; tolerate a single item for hand edits."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _resolve_pools(part, indexes, global_materials, *, outfit_modes=True):
    """Resolve optional pool refs into ``*_items`` runtime fields.

    The raw ref arrays (``scene_pool`` / ``action_pool`` / ``outfit_pool``) are
    left untouched so the WebUI can keep editing them by stable IDs; runtime
    consumers read the resolved ``*_items`` copies.
    """
    if 'scene_pool' in part:
        part['scene_pool_items'] = [_resolve_scene(ref, indexes)
                                    for ref in _refs(part['scene_pool'])]
    if 'action_pool' in part:
        part['action_pool_items'] = [_resolve_action(ref, indexes, global_materials)
                                     for ref in _refs(part['action_pool'])]
    if 'outfit_pool' in part:
        part['outfit_pool_items'] = [_resolve_outfit(ref, indexes, global_materials)
                                     for ref in _refs(part['outfit_pool'])]
    if outfit_modes:
        # 子事件内部不再有服装随机池：旧数据的 pool 降级成「固定第一套」。
        if part.get('outfit_mode') == 'pool':
            refs = _refs(part.get('outfit_pool'))
            if refs:
                part['outfit_ref'] = refs[0]
            part['outfit_mode'] = 'fixed'
            part.pop('outfit_pool', None)
        mode = part.get('outfit_mode')
        if mode is not None and mode not in OUTFIT_MODES:
            raise ValueError('invalid outfit_mode')
        if mode is None and part.get('outfit_pool_items'):
            part['outfit_mode'] = 'event'


def resolve_event(event, global_materials=None):
    """Resolve refs (and optional random pools) into inline runtime fields.

    Never mutates the editable event. Unknown/extra keys are preserved in the
    returned copy so a newer editor's experimental fields survive read paths.
    """
    _check_shape(event)
    e = deepcopy(event)
    libraries = e.get('materials', {})
    if not isinstance(libraries, dict):
        raise ValueError('event materials must be an object')
    indexes = {}
    for kind in ('scenes', 'outfits', 'actions'):
        rows = libraries.get(kind, [])
        if not isinstance(rows, list):
            raise ValueError('event library must be a list')
        index = {}
        for row in rows:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ('id', 'name', 'text')):
                raise ValueError('event material needs id, name and text')
            if row['id'] in index:
                raise ValueError('duplicate event material ID')
            index[row['id']] = row
        indexes[kind] = index
    for index, part in enumerate([e] + e.get('phases', [])):
        for field, kind in FIELDS.items():
            if field + '_ref' not in part:
                continue
            ref = part[field + '_ref']
            if kind == 'scenes':
                source, token = _ref_parts(ref, 'scenes')
                row = indexes['scenes'].get(token)
                if row is None:
                    raise ValueError('event material reference not found')
                part[field] = (dict(row['scene'], desc=row['text'])
                               if isinstance(row.get('scene'), dict) else row['text'])
            elif kind == 'outfits':
                part[field] = _resolve_outfit(ref, indexes, global_materials)
            else:
                part[field] = _resolve_action(ref, indexes, global_materials)
        _resolve_pools(part, indexes, global_materials, outfit_modes=index > 0)
        # Primaries for code paths that still read the single inline values.
        if not part.get('scene_ref') and part.get('scene_pool_items'):
            first = part['scene_pool_items'][0]
            part['scene'] = (dict(first['scene'], desc=first['text'])
                             if isinstance(first.get('scene'), dict) else first['text'])
        if not part.get('action_ref') and part.get('action_pool_items'):
            part['action'] = part['action_pool_items'][0]
        if not part.get('outfit_ref') and part.get('outfit_pool_items') and not part.get('outfit'):
            part['outfit'] = dict(part['outfit_pool_items'][0])
    return e
