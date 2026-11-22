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
                      "diaries", "schedule", "job", "cancel_job",
                      "ref_library", "save_ref_image", "delete_ref_image", "rename_ref_image",
                      "save_ref_slots", "save_ref_conditions", "ref_image",
                      "save_ref_costume", "pose_sketches", "save_pose_sketch",
                      "rename_pose_sketch", "delete_pose_sketch", "pose_sketch_image"})
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
    "素材库正在被其它操作占用，请稍后重试",
    "素材编号不存在，请刷新素材列表",
    "素材库至少要保留一条",
    "素材文件不存在，请检查素材路径",
    "素材格式无效，请检查内容后重试",
    "互动草稿要以「动作词：【机位短语】；正文」开头",
    "场景描述格式无效", "动作描述格式无效",
    "测试场景类型无效，请刷新页面后重试",
    "服装/动作来源无效，请刷新页面后重试",
    "请选择日常服装",
    "请选择参考图衣服",
    "参考图衣服只能在「参考图衣服」或「日常服装」来源里用",
    "参考图衣服格式无效，请刷新页面后重试",
    "参考图页还没有启用素颜：请到「参考图」页勾上「启用素颜」并上传一张素颜三视图",
    "还没有可用的参考图：请打开插件页「自拍工作台 → 参考图」，上传并填好主站 / 副站参考图，再回来拍照",
    "这套参考图衣服已经没有名字了，请刷新页面后重新选择",
    "这套衣服没有勾「可摘帽子」，请刷新页面后重试",
    "这套衣服没有勾「可脱外套」，请刷新页面后重试",
    "请选择线稿参考特写", "线稿特写不存在，请刷新后重试",
    "线稿参考特写不存在，请刷新线稿列表后重试",
    "线稿参考特写需要支持至少两张参考图的生图站，请检查主站或副站的参考图张数设置",
    "线稿参考特写暂不与角色参考图混用，请单独选择线稿动作",
    "请选择日常特写",
    "请选择互动提示词（先在「添加素材」里添加互动）",
    "参考图数据无效，请重新选择图片",
    "参考图格式无效，请上传 PNG / JPEG / WebP",
    "参考图过大，请压缩到 12MB 以内",
    "参考图像素过大，请压缩后再上传",
    "参考图过大，无法在页面预览",
    "参考图是空的，请重新选择图片",
    "参考图不存在，请刷新后重试",
    "参考图读取失败，请重新上传",
    "参考图名称太长（最多 40 个字）",
    "参考图编号太长（最多 40 个字）",
    "参考图设置格式无效",
    "所选参考图不存在，请刷新后重试",
    "条件参考图格式无效",
    "条件参考图要先选一张已上传的图片",
    "条件参考图引用的图片不存在，请刷新后重试",
    "条件参考图要选至少一个要替换的参考图位置",
    "条件参考图最多 50 条",
    "条件类型无效，请选择日期 / 气温 / 概率",
    "气温条件要选「低于」或「高于」",
    "气温阈值要在 -60 到 60 之间",
    "日期条件要填月份和日期",
    "日期条件的月或日无效",
    "概率要在 1 到 100 之间",
    "第二张参考图引用的图片不存在，请刷新后重试",
    "第二张参考图要先勾上「主站参考图2」或「副站参考图2」",
    "参考图操作无效，请刷新页面后重试",
    "测试参数无效，请刷新页面后重试",
    "已取消这次生图",
    "页面已关闭，已取消这次生图",
    "任务已取消",
})


def safe_error(exc):
    """Return fixed public messages only; never echo arbitrary provider/path text."""
    if type(exc).__name__ in {"WorkspaceConflict", "MaterialConflict"}:
        return "数据已变化，请刷新后重新操作"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return '工作台任务超时，请减少本次数量后重试；已有数据未改动'
    if type(exc).__name__ == "RefLibraryError":
        # 参考图库的错误都是自己写的中文短句（字段名 + 数字），可以直接给用户看。
        message = str(exc)
        if message and len(message) <= 80 and "\\" not in message and "/" not in message:
            return message
        return "参考图设置无效，请刷新页面后重试"
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
    # 付费生图任务：页面不再轮询这么久就自动取消（退出工作台/关页后不会白跑）
    UNSEEN_CANCEL = 90.0
    IMAGE_ACTIONS = frozenset({"test", "try_costume"})

    def __init__(self, host, service):
        self.host = host
        self.service = service
        self._context = None
        self._closed = False
        self._jobs = {}
        self._tasks = set()
        self._watchers = set()
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
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.JOB_TIMEOUT):
                result = await self.service.dispatch(action, payload, owner)
            if action in self.IMAGE_ACTIONS and isinstance(result, dict):
                # Whole service task: prompt preparation, retries and downloads;
                # never include the client's polling delay or mutate service data.
                result = {**result, "elapsed_seconds": time.monotonic() - started}
            # Do not retain arbitrarily large or non-JSON service responses.
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode("utf-8")) > self.MAX_JSON_BYTES:
                raise ValueError("Result limit")
            job.update(state="done", result=result)
        except asyncio.CancelledError:
            if job["state"] == "running":
                job.update(state="error", error="任务已取消")
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
               "error": None, "finished": None, "task": None,
               "seen": time.monotonic(),
               "cancel_unseen": action in self.IMAGE_ACTIONS}
        self._jobs[job_id] = job
        task = asyncio.create_task(self._run(job, action, payload, owner))
        job["task"] = task
        self._tasks.add(task)
        _ACTIVE[task] = owner
        task.add_done_callback(self._task_done)
        if job["cancel_unseen"]:
            self._watch(job_id)
        return {"job_id": job_id}

    def _watch(self, job_id):
        """付费生图：页面不再轮询（退出工作台/关页）就取消任务，别让它在后台空跑。"""
        async def watch():
            try:
                while True:
                    await asyncio.sleep(min(5.0, self.UNSEEN_CANCEL))
                    job = self._jobs.get(job_id)
                    if job is None or job["state"] != "running":
                        return
                    if time.monotonic() - job.get("seen", 0) <= self.UNSEEN_CANCEL:
                        continue
                    task = job.get("task")
                    if task is not None and not task.done():
                        job.update(state="error", error="页面已关闭，已取消这次生图",
                                   finished=time.monotonic())
                        task.cancel()
                    return
            except asyncio.CancelledError:
                raise
            finally:
                watcher = asyncio.current_task()
                if watcher is not None:
                    self._watchers.discard(watcher)

        watcher = asyncio.create_task(watch())
        self._watchers.add(watcher)

    def _cancel(self, payload, owner):
        job_id = payload.get("job_id")
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None or job["owner"] != owner:
            raise _Rejected("Job not found", 404)
        if job["state"] == "running":
            job.update(state="error", error="已取消这次生图", finished=time.monotonic())
            task = job.get("task")
            if task is not None and not task.done():
                task.cancel()
        return {"job_id": job_id, "state": job["state"]}

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
                if job["state"] == "running":
                    job["seen"] = time.monotonic()
                data = {key: job[key] for key in ("state", "result", "error")}
            elif action == "cancel_job":
                data = self._cancel(payload, owner)
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
        watchers = tuple(self._watchers)
        for task in tasks:
            task.cancel()
        for watcher in watchers:
            watcher.cancel()
        try:
            if tasks or watchers:
                await asyncio.gather(*tasks, *watchers, return_exceptions=True)
        finally:
            for task in tasks:
                if task.done():
                    self._task_done(task)
            self._watchers.clear()
            self._jobs.clear()
