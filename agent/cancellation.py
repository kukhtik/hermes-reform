"""Cancellation registry for in-flight httpx streams.

When the user invokes /stop (or a Stop event fires), all registered in-flight
HTTP streams are cancelled via stream.aclose() / stream.close() to stop
provider API calls immediately rather than letting them complete wastefully.

Registry is thread-safe and idempotent — cancel_all() is safe to call multiple
times and from any thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Thread-safe set of registered streams.  Each entry is the stream object;
# the registry calls stream.close() (sync) for cancellation.
_REGISTRY: set[Any] = set()
_REGISTRY_LOCK = threading.Lock()

# Track live fire-and-forget tasks so they are never silently discarded.
# Python logs "Task exception never retrieved" when a task fails and its
# result is never awaited — adding a done-callback that discards + logs
# suppresses that warning while keeping the task alive until it finishes.
_LIVE_TASKS: set[asyncio.Task[Any]] = set()
_LIVE_TASKS_LOCK = threading.Lock()


def _discard_and_log(task: asyncio.Task[Any]) -> None:
    """Done-callback: suppress 'Task exception never retrieved' warning.

    Without this, any task whose coroutine raises (e.g. aclose() on a
    stream that is already closed) would produce a logged exception when
    the task is garbage-collected without being awaited.
    """
    try:
        _ = task.result()
    except BaseException as exc:
        logger.debug("fire-and-forget aclose() task raised: %s", exc)


def _track_task(task: asyncio.Task[Any]) -> None:
    with _LIVE_TASKS_LOCK:
        _LIVE_TASKS.add(task)
    task.add_done_callback(_discard_and_log)
    # Keep only a bounded set; remove on done to avoid unbounded growth
    # across many cancel_all() calls in long-running processes.
    task.add_done_callback(
        lambda t: _LIVE_TASKS.discard(t)
        if t in _LIVE_TASKS
        else None
    )


def register_stream(stream: Any) -> None:
    """Register an in-flight httpx stream for cancellation on /stop.

    Idempotent — re-registering the same stream object is safe.
    """
    with _REGISTRY_LOCK:
        _REGISTRY.add(stream)


def unregister_stream(stream: Any) -> None:
    """Unregister a stream (e.g. after successful completion).

    Idempotent — unregistering an already-unregistered stream is a no-op.
    """
    with _REGISTRY_LOCK:
        _REGISTRY.discard(stream)


def close_managed_stream(stream: Any) -> None:
    """Unregister a stream from the cancellation registry without closing it.

    This is the SUCCESS-PATH cleanup for ManagedLlmStream: after a stream is
    fully consumed, call this to remove it from the registry WITHOUT invoking
    stream.close() — which would tear down the httpx connection pool and cause
    the next request to hang.

    Only cancel_all() (user /stop signal) is allowed to call stream.close().
    """
    unregister_stream(stream)


def cancel_all() -> None:
    """Cancel all registered in-flight streams (synchronous, thread-safe).

    Calls ``stream.close()`` on every registered stream.  This is the
    synchronous cancellation path used from signal handlers and the
    conversation loop's stop-signal hook.

    Idempotent — safe to call even if some streams are already closed.
    """
    with _REGISTRY_LOCK:
        streams = list(_REGISTRY)
        _REGISTRY.clear()

    for stream in streams:
        try:
            loop = asyncio.get_event_loop()
        except Exception:
            loop = None

        # If an async loop is running, prefer aclose() fire-and-forget over
        # blocking sync close().  ManagedLlmStream.close() itself calls
        # run_until_complete() internally and would block this thread.
        if loop is not None and callable(getattr(stream, "aclose", None)):
            try:
                if loop.is_running():
                    task = asyncio.ensure_future(stream.aclose())
                    _track_task(task)
                    continue
            except Exception:
                pass

        # Sync path: call stream.close() directly.
        # Falls through for streams without acloase, or when no loop is running.
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


async def cancel_all_async() -> None:
    """Async cancellation path — calls stream.aclose() on all registered streams.

    Prefer this from async contexts where awaiting is acceptable.
    """
    with _REGISTRY_LOCK:
        streams = list(_REGISTRY)
        _REGISTRY.clear()

    for stream in streams:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                pass
