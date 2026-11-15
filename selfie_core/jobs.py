"""Task ownership and an injectable, per-instance serialized photo use case.

These objects belong to one event loop. Neither is a cross-process coordinator.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect


class TaskSupervisor:
    """Own tasks until completion and retrieve failures without logging secrets.

    on_error is a synchronous callback receiving the fixed string 'Task failed',
    never exception text/objects or coroutine arguments. Callback errors are
    consumed too. Awaiting a returned task still observes its original failure.
    """

    def __init__(self, on_error=None):
        self._tasks = set()
        self._closed = False
        self._on_error = on_error

    @property
    def closed(self):
        return self._closed

    @property
    def tasks(self):
        return frozenset(self._tasks)

    def spawn(self, coro) -> asyncio.Task:
        if self._closed:
            if inspect.iscoroutine(coro):
                coro.close()
            raise RuntimeError("Task supervisor is closed")
        if not inspect.iscoroutine(coro):
            raise TypeError("spawn requires a coroutine object")
        try:
            task = asyncio.create_task(coro)
        except BaseException:
            coro.close()
            raise
        self._tasks.add(task)
        task.add_done_callback(self._done)
        return task

    def _done(self, task):
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()  # Always retrieve errors, even without a callback.
        if error is not None and self._on_error is not None:
            try:
                result = self._on_error("Task failed")
                # Async callbacks are not supported: avoid leaking a coroutine.
                if inspect.iscoroutine(result):
                    result.close()
            except Exception:
                pass

    async def close(self):
        """Reject new work, cancel and await existing tasks; safe to repeat.

        Call from the owner, not a supervised task (self-await is impossible).
        Cancellation of close propagates after gather cancels its children.
        """
        current = asyncio.current_task()
        if current in self._tasks:
            raise RuntimeError("A supervised task cannot close its own supervisor")
        self._closed = True
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            try:
                await asyncio.gather(*pending, return_exceptions=True)
            finally:
                for task in pending:
                    if task.done():
                        self._tasks.discard(task)


@dataclass(frozen=True)
class PhotoRequest:
    prompt: str
    model: str
    style: str | None = None
    negative_extra: tuple[str, ...] = ()
    extra_images: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.prompt, str) or not isinstance(self.model, str):
            raise TypeError("prompt and model must be strings")
        if self.style is not None and not isinstance(self.style, str):
            raise TypeError("style must be a string or None")
        for name in ("negative_extra", "extra_images"):
            values = getattr(self, name)
            if values is None:
                values = ()
            if isinstance(values, (str, bytes)):
                raise TypeError(name + " must be a sequence of strings")
            snapshot = tuple(values)
            if not all(isinstance(value, str) for value in snapshot):
                raise TypeError(name + " must contain only strings")
            object.__setattr__(self, name, snapshot)


class PhotoUseCase:
    """Serialize generation + optional storage per shared instance.

    generator is async and accepts the legacy _generate keyword arguments.
    store(request, image_bytes) may be synchronous or async. Storage is not a
    transaction: its failures propagate, and any side effects already performed
    cannot be undone. Cancellation is never swallowed; the lock is released.
    """

    def __init__(self, generator, store=None):
        self._generator = generator
        self._store = store
        self._lock = asyncio.Lock()

    async def execute(self, request: PhotoRequest) -> bytes:
        if not isinstance(request, PhotoRequest):
            raise TypeError("execute requires a PhotoRequest")
        async with self._lock:
            result = await self._generator(
                prompt=request.prompt, model=request.model, style=request.style,
                negative_extra=list(request.negative_extra) or None,
                extra_images=list(request.extra_images) or None,
            )
            if not isinstance(result, bytes):
                raise TypeError("Photo generator must return bytes")
            if self._store is not None:
                stored = self._store(request, result)
                if inspect.isawaitable(stored):
                    await stored
            return result
