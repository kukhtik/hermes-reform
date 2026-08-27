"""Cancellation registry for in-flight httpx streams.

When the user invokes /stop (or a Stop event fires), all registered in-flight
HTTP streams are cancelled via stream.aclose() / stream.close() to stop
provider API calls immediately rather than letting them complete wastefully.

Registry is thread-safe and idempotent — cancel_all() is safe to call multiple
times and from any thread.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

# Thread-safe set of registered streams.  Each entry is the stream object;
# the registry calls stream.close() (sync) for cancellation.
_REGISTRY: set[Any] = set()
_REGISTRY_LOCK = threading.Lock()


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
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        else:
            # Fallback: try aclose in a fire-and-forget task if we're in async
            aclose = getattr(stream, "aclose", None)
            if callable(aclose):
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(aclose())
                    else:
                        loop.run_until_complete(aclose())
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
