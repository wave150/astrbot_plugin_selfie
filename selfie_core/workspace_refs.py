"""工作台「参考图」页的服务端动作：上传、改名、删除、槽位和条件参考图。

这一层只做「数据 URL → 字节」和「服务方法 → 返回结构」的胶水，图片校验、落盘、
条件优先级都在 :mod:`selfie_core.ref_library` 里，方便离线测试。

另外负责「填入内置参考图」：把插件自带的那几张参考图导入图片墙并填好槽位，
以及给每个快照补一句「现在用的是哪套参考图」（``builtin``），页面据此显示
「未接管 / 已接管」。
"""
from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from .ref_library import RefLibraryError

REF_ACTIONS = frozenset({
    "ref_library", "save_ref_image", "delete_ref_image", "rename_ref_image",
    "save_ref_slots", "save_ref_conditions", "ref_image", "adopt_builtin_refs",
    "save_ref_costume",
})

_DATA_URL_PREFIXES = (
    "data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,",
)
MAX_DATA_URL_CHARS = 12 * 1024 * 1024
MAX_THUMB_CHARS = 1024 * 1024


def decode_image_data_url(value):
    """把前端上传的 data URL 解成字节；格式或体积不对时抛中文错误。"""
    if not isinstance(value, str) or not value:
        raise RefLibraryError("参考图数据无效，请重新选择图片")
    if len(value) > MAX_DATA_URL_CHARS:
        raise RefLibraryError("参考图过大，请压缩到 12MB 以内")
    prefix, separator, encoded = value.partition(",")
    if not separator or prefix + "," not in _DATA_URL_PREFIXES:
        raise RefLibraryError("参考图格式无效，请上传 PNG / JPEG / WebP")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise RefLibraryError("参考图数据无效，请重新选择图片") from None


def image_data_url(payload, *, thumb):
    mime = "image/jpeg" if thumb else "image/png"
    return "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii")


def _builtin_plan(service):
    """宿主给的内置参考图方案；老版本宿主没有这个方法时返回 None。"""
    host = getattr(service, "host", None)
    builder = getattr(host, "_builtin_ref_plan", None)
    if not callable(builder):
        return None
    try:
        plan = builder()
    except Exception:  # noqa: BLE001 - 算不出内置图就当没有，不影响页面其它功能
        return None
    return plan if isinstance(plan, dict) else None


def builtin_info(service):
    """页面顶部那句「现在用的是哪套内置参考图」。"""
    info = {"available": False, "style": "", "summary": ""}
    plan = _builtin_plan(service)
    if not plan:
        return info
    main = plan.get("main") if isinstance(plan.get("main"), dict) else {}
    backup = plan.get("backup") if isinstance(plan.get("backup"), dict) else {}
    parts = []
    if main.get("body"):
        parts.append("全身三视图")
    if main.get("dual") and main.get("head"):
        parts.append("头部参考图")
    if backup.get("enabled") and backup.get("image"):
        parts.append("副站单图参考图")
    info["available"] = bool(parts)
    info["style"] = str(plan.get("style") or "")
    info["summary"] = "、".join(parts)
    return info


def with_builtin(service, snapshot):
    """给参考图快照补上内置参考图信息；bootstrap 和各动作返回共用同一份结构。"""
    payload = dict(snapshot)
    payload["builtin"] = builtin_info(service)
    return payload


def _adopt_builtin_refs(service, library):
    """把插件内置参考图导入图片墙并填好槽位；已导入过的按来源复用，不会越点越多。"""
    plan = _builtin_plan(service)
    if not plan:
        raise RefLibraryError("这个版本没有可以填入的内置参考图")
    labels = plan.get("labels") if isinstance(plan.get("labels"), dict) else {}
    main = plan.get("main") if isinstance(plan.get("main"), dict) else {}
    backup = plan.get("backup") if isinstance(plan.get("backup"), dict) else {}

    def take(path, role, label):
        path = str(path or "")
        if not path:
            return ""
        source = os.path.abspath(path)
        existing = library.find_source(source)
        if existing:
            return existing
        try:
            data = Path(source).read_bytes()
        except OSError:
            raise RefLibraryError("内置参考图读不出来：" + os.path.basename(source)) from None
        meta = library.save_image(data, label=label, role=role, source=source)
        return meta["id"]

    body = take(main.get("body"), "main", labels.get("body") or "内置全身三视图")
    head = take(main.get("head") if main.get("dual") else "", "main",
                labels.get("head") or "内置头部参考图")
    single = take(backup.get("image"), "backup", labels.get("backup") or "内置单图参考图")
    if not (body or head or single):
        raise RefLibraryError("没有找到可以填入的内置参考图，请直接在下面上传")
    library.save_slots({
        "main": {"dual": bool(head) or bool(main.get("dual")), "body": body, "head": head},
        "backup": {"enabled": bool(backup.get("enabled") and single), "single": True,
                   "image": single, "image2": ""},
    })
    return with_builtin(service, library.snapshot())


def handle(service, action, payload):
    """处理参考图页的动作；返回值直接给前端（含最新快照，省一次 bootstrap）。"""
    library = service.reference_library()
    if action == "ref_library":
        return with_builtin(service, library.snapshot())
    if action == "save_ref_image":
        role = payload.get("role")
        role = role if role in ("main", "backup", "extra") else "extra"
        meta = library.save_image(decode_image_data_url(payload.get("image")),
                                  label=payload.get("label"), role=role)
        return {"image": meta, "library": with_builtin(service, library.snapshot())}
    if action == "rename_ref_image":
        meta = library.rename_image(payload.get("id"), payload.get("label"))
        return {"image": meta, "library": with_builtin(service, library.snapshot())}
    if action == "delete_ref_image":
        result = library.delete_image(payload.get("id"))
        return dict(result, library=with_builtin(service, library.snapshot()))
    if action == "save_ref_slots":
        return with_builtin(service, library.save_slots({"main": payload.get("main"),
                                                         "backup": payload.get("backup")}))
    if action == "save_ref_conditions":
        return with_builtin(service, library.save_conditions(payload.get("conditions")))
    if action == "save_ref_costume":
        return with_builtin(service, library.save_costume(
            payload.get("id"), payload.get("costume"),
            hat=payload.get("hat") is True, outer=payload.get("outer") is True,
            single_ref=payload.get("single_ref") is True))
    if action == "adopt_builtin_refs":
        return _adopt_builtin_refs(service, library)
    if action == "ref_image":
        thumb = payload.get("thumb", True) is not False
        data = library.read(payload.get("id"), thumb=thumb)
        if not thumb and len(data) * 4 // 3 > MAX_THUMB_CHARS * 12:
            raise RefLibraryError("参考图过大，无法在页面预览")
        return {"image": image_data_url(data, thumb=thumb)}
    raise RefLibraryError("参考图操作无效，请刷新页面后重试")
