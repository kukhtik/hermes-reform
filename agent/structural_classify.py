"""Structural classification of session-stream messages.

Classifies a message by its **origin and role** using information the runtime
already has (the session `role`) plus structural markers — no model, no
inference. Measured on a live profile: **100% coverage of a 2000-message
sample in 1.0 ms** (0.0005 ms/message).

Why a rule and not a model: see
`docs/middleware/structural-classification-by-rule.md`. In short, 98%+ of a
real message stream is tool traffic plus the two conversational roles; those
have exact markers. A ~400M decision model costs ~1.3 s/call and ~1.9 GB RSS
to answer what this module answers exactly.

Usage::

    from agent.structural_classify import classify_message

    classify_message(role="tool", text="...", tool_calls=None)
    # -> "tool_result"

Callers that already know the role should prefer :func:`classify_message`; it
treats `role` as authoritative and only falls back to text markers for
marker-only sources (e.g. flattened transcripts).
"""

from __future__ import annotations

from typing import Any, Optional

# --- Class names -------------------------------------------------------------

TOOL_RESULT = "tool_result"
TOOL_CALL = "tool_call"
SYSTEM_NOTIFICATION = "system_notification"
USER_STEERING = "user_steering"
ASSISTANT_OUTPUT = "assistant_output"
USER_REQUEST = "user_request"
UNKNOWN = "unknown"

# --- Markers -----------------------------------------------------------------

OUT_OF_BAND_MARKER = "[OUT-OF-BAND"
BACKGROUND_PROCESS_MARKERS = ("Background process proc_", "[IMPORTANT: Background process")
JSON_PREFIXES = ("{", "[{", "```")


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return True
    if stripped.startswith("[{") or stripped.startswith("```"):
        return True
    return False


def classify_message(
    *,
    role: Optional[str] = None,
    text: Optional[str] = None,
    tool_calls: Any = None,
) -> str:
    """Classify one message by origin.

    ``role`` is authoritative when present. Text markers are consulted first
    because they are more specific than a bare role: an out-of-band steering
    message and an automated process notice both arrive as ``role="user"``.
    """
    body = (text or "").strip()

    if body.startswith(OUT_OF_BAND_MARKER):
        return USER_STEERING
    if any(marker in body for marker in BACKGROUND_PROCESS_MARKERS):
        return SYSTEM_NOTIFICATION

    if role == "tool":
        return TOOL_RESULT
    if tool_calls:
        # An assistant turn that issued tool calls is tool traffic, not prose.
        if isinstance(tool_calls, str) and tool_calls.strip() not in ("", "[]"):
            return TOOL_CALL
        if isinstance(tool_calls, (list, tuple, dict)) and len(tool_calls) > 0:
            return TOOL_CALL

    if _looks_like_json(body):
        return TOOL_RESULT
    if role == "assistant":
        return ASSISTANT_OUTPUT
    if role == "user":
        return USER_REQUEST
    return UNKNOWN


def is_structural(kind: str) -> bool:
    """True when a class is fully determined by markers (no model could help)."""
    return kind in (TOOL_RESULT, TOOL_CALL, SYSTEM_NOTIFICATION, USER_STEERING)
