"""工作台「参考图」页的服务端动作：上传、改名、删除、槽位和条件参考图。

这一层只做「数据 URL → 字节」和「服务方法 → 返回结构」的胶水，图片校验、落盘、
条件优先级都在 :mod:`selfie_core.ref_library` 里，方便离线测试。

新版本不再内置任何默认参考图：所有图片都由用户在这个页面上传。
"""
from __future__ import annotations

import base64
import binascii

from .ref_library import RefLibraryError

REF_ACTIONS = frozenset({
    "ref_library", "save_ref_image", "delete_ref_image", "rename_ref_image",
    "save_ref_slots", "save_ref_conditions", "ref_image", "save_ref_costume",
})

POSE_SKETCH_ACTIONS = frozenset({
    "pose_sketches", "save_pose_sketch", "rename_pose_sketch",
    "delete_pose_sketch", "pose_sketch_image",
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


def with_builtin(service, snapshot):
    """参考图快照。

    旧版本这里会补「内置参考图」信息，现在不再内置任何默认图，直接返回快照；
    保留这个函数名是为了 workspace.bootstrap 不用改调用点。
    """
    return dict(snapshot)


def handle(service, action, payload):
    """处理参考图页的动作；返回值直接给前端（含最新快照，省一次 bootstrap）。"""
    if action in POSE_SKETCH_ACTIONS:
        library = service.pose_sketch_library()
        if action == "pose_sketches":
            return library.snapshot()
        if action == "pose_sketch_image":
            thumb = payload.get("thumb", True) is not False
            return {"image": image_data_url(library.read(payload.get("id"), thumb=thumb), thumb=thumb)}
        if action == "save_pose_sketch":
            meta = library.save_image(decode_image_data_url(payload.get("image")), label=payload.get("label"))
            return {"image": meta, "pose_sketches": library.snapshot()}
        if action == "rename_pose_sketch":
            meta = library.rename_image(payload.get("id"), payload.get("label"))
            return {"image": meta, "pose_sketches": library.snapshot()}
        result = library.delete_image(payload.get("id"))
        return dict(result, pose_sketches=library.snapshot())
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
        slots = {"main": payload.get("main"), "backup": payload.get("backup")}
        if "suyan" in payload:
            slots["suyan"] = payload.get("suyan")
        return with_builtin(service, library.save_slots(slots))
    if action == "save_ref_conditions":
        return with_builtin(service, library.save_conditions(payload.get("conditions")))
    if action == "save_ref_costume":
        return with_builtin(service, library.save_costume(
            payload.get("id"), payload.get("costume"),
            hat=payload.get("hat") is True, outer=payload.get("outer") is True,
            single_ref=payload.get("single_ref") is True))
    if action == "ref_image":
        thumb = payload.get("thumb", True) is not False
        data = library.read(payload.get("id"), thumb=thumb)
        if not thumb and len(data) * 4 // 3 > MAX_THUMB_CHARS * 12:
            raise RefLibraryError("参考图过大，无法在页面预览")
        return {"image": image_data_url(data, thumb=thumb)}
    raise RefLibraryError("参考图操作无效，请刷新页面后重试")
