"""Authenticated AstrBot bridge, bounded in-memory jobs; no standalone server.

The platform authenticates requests. This adapter additionally requires its
username and never accepts an owner supplied by the client. Limits apply after
platform request buffering; upstream HTTP body limits remain recommended.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
import weakref

from astrbot.api.web import request, json_response, error_response

_ROUTE = "/astrbot_plugin_selfie/workspace"
_ACTIONS = frozenset({"bootstrap", "test", "try_costume", "draft_material", "confirm_material",
                      "initialize_world", "update_material", "delete_material",
                      "draft_city", "confirm_city", "add_city", "save_world", "save_events",
                      "lookup_city", "generate_event_fragment",
                      "album_list", "album_thumb", "album_image",
                      "diaries", "schedule", "job"})
_BACKGROUND = frozenset({"test", "try_costume", "draft_material", "draft_city", "initialize_world",
                         "generate_event_fragment"})
# Process-level admission, including overlapping plugin instances during reload.
_ACTIVE = weakref.WeakKeyDictionary()


_SAFE_MESSAGES = frozenset({
    '结束后场景已关闭，无法测试',
    '大模型响应超时（单次上限180秒），请减少本次地点数量后重试；已有地点未改动',
    '无法连接大模型服务，请检查服务状态后重试；已有数据未改动',
    '大模型调用失败，请检查 AstrBot 模型配置、额度及服务状态后重试；已有数据未改动',
    '事件素材引用无效，请检查场景库、服装库和动作库',
    '城市草稿不符合要求，请减少数量或调整生成要求后重试；已有地点未改动',
    *('城市草稿校验失败：' + category + '；请调整数量或重新生成，已有地点未改动' for category in (
        'JSON格式无效', '地点与已有或本批内容重复', '缺少要求的夜晚住宿地点',
        '地点数量不符合本次要求', '缺少要求的景点或区县覆盖', '区县、邻接或地点结构无效')),
    "不存在该城市，请重新输入",
    "城市不存在或名称有歧义，请检查城市及省份",
    "已有生活或旅行状态，修改家庭位置前请先备份并显式迁移或重置世界状态",
    "请先在 AstrBot 配置可用的大模型",
    "工作台输入无效，请检查必填项、城市区划或草稿格式",
    "请确认本次测试会调用生图服务",
    "衣服号或特写号不存在，请刷新素材列表",
    "所选城市、区或地点已不存在",
    "正在生成照片，请稍后测试",
    "请先确认本次操作可能产生费用", "图片不存在或已被清理",
    "参考图过大", "参考图必须为 JPEG、PNG 或 WebP", "参考图编码无效", "参考图像素过大",
    "模型没有返回可用内容，请重试",
    "行政区数据缺少可靠来源元数据", "行政区数据缺少来源URL或时间",
    "行政区数据不可用，禁止由LLM猜测城市",
    "缺少可靠的多个区县数据，不能生成城市",
    "省份间无陆地邻接路径，不能虚构跨海路线",
    "该城市已在世界表里，请直接生成地点草稿",
    "城市地点不生成过夜点（酒店/青旅/旅馆/网吧），过夜由固定场景处理",
    "每个区至少要有1个景点（公园/景区/古迹/博物馆等），且整座城市景点不少于区县数",
    "事件不存在或已被删除，请刷新事件列表",
    "该事件没有场景描述，请先在事件编辑里填写或 AI 生成",
    "该事件没有动作描述，请先在事件编辑里填写或 AI 生成",
    "该事件没有设置服装，请先在事件编辑里选择衣服",
    "请选择事件服装",
    "该事件没有这件服装，请刷新事件列表后再试",
    "该事件服装没有描述，请先选择衣服库或 AI 生成专属服装",
    "该房间没有环境描述，请先在「世界设置」里填写房间描述",
    "过夜场景不存在，请刷新过夜列表",
    "请先在「首次设定家」里设定家所在城市并生成地点",
    "这座城市已经有地点了，请直接用「重新生成城市」",
    "家区县必须属于该城市，请重新选择",
    "素材内容或名称与已有条目重复",
    "素材编号不存在，请刷新素材列表",
    "素材库至少要保留一条",
    "素材文件不存在，请检查素材路径",
    "素材格式无效，请检查内容后重试",
    "场景描述格式无效", "动作描述格式无效",
    "测试场景类型无效，请刷新页面后重试",
    "服装/动作来源无效，请刷新页面后重试",
    "请选择日常服装",
    "请选择日常特写",
    "测试参数无效，请刷新页面后重试",
})


def safe_error(exc):
    """Return fixed public messages only; never echo arbitrary provider/path text."""
    if type(exc).__name__ in {"WorkspaceConflict", "MaterialConflict"}:
        return "数据已变化，请刷新后重新操作"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return '工作台任务超时，请减少本次数量后重试；已有数据未改动'
    if isinstance(exc, ValueError):
        message = str(exc)
        if message in _SAFE_MESSAGES:
            return message
        return "工作台输入无效，请检查必填项、城市区划或草稿格式"
    return "Workspace operation failed"


class _Rejected(Exception):
    def __init__(self, message, code=400):
        self.message, self.code = message, code


class WorkspaceAPI:
    MAX_JSON_BYTES = 16 * 1024 * 1024
    MAX_IMAGE_CHARS = 12 * 1024 * 1024
    MAX_JOBS = 100
    JOB_TTL = 15 * 60
    JOB_TIMEOUT = 12 * 60
    MAX_RUNNING = 3

    def __init__(self, host, service):
        self.host = host
        self.service = service
        self._context = None
        self._closed = False
        self._jobs = {}
        self._tasks = set()
        self._handler = self.handle  # Stable identity for replacement-safe teardown.

    def register(self, context):
        if self._closed:
            raise RuntimeError("Workspace API is closed")
        if self._context is not None and self._context is not context:
            raise RuntimeError("Workspace API is already registered")
        context.register_web_api(_ROUTE, self._handler, ["POST"],
                                 "Authenticated selfie workspace")
        self._context = context

    @classmethod
    def _validate(cls, value, depth=0):
        if depth > 12:
            raise _Rejected("Payload nesting limit exceeded")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise _Rejected("Invalid JSON object")
                cls._validate(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                cls._validate(child, depth + 1)
        elif isinstance(value, str):
            # Bound all strings, so alternate image field names cannot bypass it.
            if len(value) > cls.MAX_IMAGE_CHARS:
                raise _Rejected("Image or text field is too large", 413)
        elif value is None or isinstance(value, (bool, int)):
            pass
        elif isinstance(value, float) and math.isfinite(value):
            pass
        else:
            raise _Rejected("Invalid JSON value")

    def _expire(self):
        now = time.monotonic()
        for job_id, job in tuple(self._jobs.items()):
            if job["state"] != "running" and now - job["finished"] >= self.JOB_TTL:
                del self._jobs[job_id]

    async def _run(self, job, action, payload, owner):
        try:
            async with asyncio.timeout(self.JOB_TIMEOUT):
                result = await self.service.dispatch(action, payload, owner)
            # Do not retain arbitrarily large or non-JSON service responses.
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > self.MAX_JSON_BYTES:
                raise ValueError("Result limit")
            job.update(state="done", result=result)
        except asyncio.CancelledError:
            job.update(state="error", error="Job cancelled")
            raise
        except Exception as exc:
            job.update(state="error", error=safe_error(exc))
        finally:
            job["finished"] = time.monotonic()

    def _start(self, action, payload, owner):
        active = [(task, user) for task, user in tuple(_ACTIVE.items()) if not task.done()]
        if any(user == owner for _, user in active):
            raise _Rejected("A job is already running for this user", 429)
        if len(active) >= self.MAX_RUNNING:
            raise _Rejected("Workspace is busy", 429)
        if len(self._jobs) >= self.MAX_JOBS:
            raise _Rejected("Job capacity reached; retry after expiry", 429)
        job_id = uuid.uuid4().hex
        job = {"owner": owner, "state": "running", "result": None,
               "error": None, "finished": None}
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run(job, action, payload, owner))
        self._tasks.add(task)
        _ACTIVE[task] = owner
        task.add_done_callback(self._task_done)
        return {"job_id": job_id}

    def _task_done(self, task):
        self._tasks.discard(task)
        _ACTIVE.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def handle(self):
        try:
            if self._closed:
                raise _Rejected("Workspace is closed", 503)
            owner = request.username
            if not isinstance(owner, str) or not owner.strip():
                raise _Rejected("Authentication required", 401)
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    length = int(length)
                except (TypeError, ValueError):
                    raise _Rejected("Invalid Content-Length") from None
                if length < 0 or length > self.MAX_JSON_BYTES:
                    raise _Rejected("Request is too large", 413)
            raw = await request.body()
            if len(raw) > self.MAX_JSON_BYTES:
                raise _Rejected("Request is too large", 413)
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise _Rejected("Expected JSON object")
            action = body.get("action")
            if not isinstance(action, str) or action not in _ACTIONS:
                raise _Rejected("Unknown workspace action")
            payload = {key: value for key, value in body.items() if key != "action"}
            self._validate(payload)
            # Explicit roundtrip detaches mutable service/mock input and enforces JSON size.
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > self.MAX_JSON_BYTES:
                raise _Rejected("Payload is too large", 413)
            payload = json.loads(encoded)
            self._expire()
            if action in {"test", "try_costume"} and payload.get("confirm_paid") is not True:
                raise _Rejected("Paid test requires explicit confirmation")
            if action == "job":
                job_id = payload.get("job_id")
                job = self._jobs.get(job_id) if isinstance(job_id, str) else None
                if job is None or job["owner"] != owner:
                    raise _Rejected("Job not found", 404)
                data = {key: job[key] for key in ("state", "result", "error")}
            elif action in _BACKGROUND:
                data = self._start(action, payload, owner)
            else:
                data = await self.service.dispatch(action, payload, owner)
            return json_response({"status": "ok", "data": data})
        except _Rejected as exc:
            return error_response(exc.message, status_code=exc.code)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            conflict = type(exc).__name__ in {"WorkspaceConflict", "MaterialConflict"}
            code = 409 if conflict else (400 if isinstance(exc, ValueError) else 500)
            return error_response(safe_error(exc), status_code=code)

    async def close(self):
        self._closed = True
        if self._context is not None:
            registry = self._context.registered_web_apis
            registry[:] = [entry for entry in registry
                           if not (entry[0] == _ROUTE and entry[1] is self._handler)]
            self._context = None
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if task.done():
                    self._task_done(task)
            self._jobs.clear()
