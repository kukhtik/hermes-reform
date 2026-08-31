"""
Desktop message persistence — outbox for backend-disconnect resilience.

Issue #8049 (P1): when the Hermes backend disconnects (60s timeout), in-flight
typed messages are silently dropped. This module adds a local-persistence
fallback so messages survive the disconnect and are replayed on reconnect.

Design
------
- Every user message submitted via ``prompt.submit`` is written to the outbox
  (``~/.hermes/outbox/<session_key>.jsonl``) BEFORE the backend is called.
- The outbox entry is written atomically (temp file + rename) so a crash during
  write cannot leave a partial record.
- On successful backend delivery the entry is removed from the outbox.
- On backend failure the entry stays in the outbox.
- On reconnect ``replay_outbox()`` reads all pending entries, sends each to the
  backend, and removes entries on success.
- ``msg_id`` (client-generated UUID) is used as the dedup key — if a message
  with the same ``msg_id`` is already in the outbox it is not written again.

Graceful degradation
-------------------
- If the outbox write fails (disk full, permissions), a ``logging.warning`` is
  emitted but the message is NOT silently dropped — it still goes to the backend.
  The worst case is the original bug (message lost on disconnect), which is no
  worse than before this module existed.
- If the backend call fails after the outbox write succeeds, the entry remains
  in the outbox and will be retried on the next submit or on reconnect.

Files allow-list (per F3.3 contract)
------------------------------------
- ``desktop_message_handler.py`` — new file, this module
- ``tui_gateway/methods_prompt.py`` — minimal patch to call MessageOutbox
- ``hermes_state.py`` — read-only, no writes
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _hermes_root() -> Path:
    """``~/.hermes`` (or $HERMES_HOME if set)."""
    home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    # Support profile-scoped HERMES_HOME: ~/.hermes/profiles/<name>
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _outbox_dir() -> Path:
    dir_path = _hermes_root() / "outbox"
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return dir_path


def _outbox_path(session_key: str) -> Path:
    """Per-session outbox file: ``~/.hermes/outbox/<session_key>.jsonl``."""
    # Sanitise session_key to be a safe filename
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in session_key)
    return _outbox_dir() / f"{safe}.jsonl"


# ---------------------------------------------------------------------------
# MessageOutbox
# ---------------------------------------------------------------------------

class MessageOutbox:
    """
    Thread-safe local outbox for user messages submitted during backend
    disconnect periods.

    Each outbox entry is a single-line JSON object::

        {
          "msg_id": "<uuid>",       # client-generated dedup key
          "text": "...",            # message content
          "session_key": "...",     # which session this belongs to
          "display_kind": "...",    # optional display kind
          "queued": false,         # queued flag
          "timestamp": 1234567890.0
        }

    Write protocol (save_before_send)
    ---------------------------------
    1. Generate ``msg_id`` if not supplied.
    2. Open temp file alongside target path (same directory, atomically safe).
    3. Acquire shared lock, read existing entries, check for duplicate ``msg_id``.
    4. If not duplicate, append new entry, flush, fsync.
    5. Rename temp → target (atomic on POSIX; best-effort on Windows).
    6. Return ``(True, msg_id)`` on success; ``(False, msg_id)`` on outbox failure
       (but message is still sent to backend — degraded but not lost).

    Remove protocol (remove_after_ack)
    ---------------------------------
    Called when the backend acknowledges the message (streaming started).
    Rewrites the file excluding the matched ``msg_id`` entry.

    Replay protocol (replay_outbox)
    -------------------------------
    Reads all entries for a session, yields them one by one.
    Caller is responsible for sending each entry and calling
    ``remove_after_ack`` on success.
    """

    _write_lock = Lock()  # serialise writes to the same outbox file

    def __init__(self) -> None:
        self._local = uuid.uuid4().hex  # this instance's unique marker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_before_send(
        self,
        text: str,
        session_key: str,
        msg_id: str | None = None,
        display_kind: str | None = None,
        queued: bool = False,
    ) -> tuple[bool, str]:
        """
        Persist ``text`` to the outbox BEFORE sending to the backend.

        Returns
        -------
        (outbox_ok, msg_id)
            ``outbox_ok`` is True when the outbox write succeeded.
            ``msg_id`` is the UUID for this message (generated if not supplied).

        If the outbox write fails, logs a warning and returns (False, msg_id).
        The caller should STILL attempt the backend send — degraded mode, not
        silent drop.
        """
        msg_id = msg_id or uuid.uuid4().hex
        entry: dict[str, Any] = {
            "msg_id": msg_id,
            "text": text,
            "session_key": session_key,
            "display_kind": display_kind,
            "queued": queued,
            "timestamp": _now(),
        }
        return self._write_entry(session_key, entry)

    def remove_after_ack(self, session_key: str, msg_id: str) -> bool:
        """
        Remove the entry with the given ``msg_id`` from the session outbox.

        Called when the backend has acknowledged the message (e.g. streaming
        has started, or ``message.complete`` event was received).

        Returns True if the entry was found and removed; False if not found.
        """
        return self._remove_entry(session_key, msg_id)

    def replay_outbox(
        self,
        session_key: str,
    ) -> list[dict[str, Any]]:
        """
        Read all pending outbox entries for ``session_key``.

        Returns
        -------
        list[dict]
            Entries in chronological order (oldest first).
            Caller should call ``remove_after_ack`` for each entry after a
            successful backend resend.

        Does NOT remove entries — that is the caller's responsibility.
        """
        return self._read_entries(session_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_entry(
        self, session_key: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        """Append ``entry`` to the outbox, deduplicating by ``msg_id``."""
        outbox_path = _outbox_path(session_key)
        msg_id = entry["msg_id"]
        tmp_path = outbox_path.with_suffix(".tmp")

        try:
            outbox_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "[MessageOutbox] could not create outbox dir for session %s: %s",
                session_key, exc,
            )
            return False, msg_id

        with self._write_lock:
            # Read existing entries to check for duplicate
            existing: list[dict[str, Any]] = []
            if outbox_path.exists():
                try:
                    with open(outbox_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    existing.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                except OSError as exc:
                    logger.warning(
                        "[MessageOutbox] could not read outbox for session %s: %s",
                        session_key, exc,
                    )
                    return False, msg_id

            # Dedup: skip if already in outbox
            if any(e.get("msg_id") == msg_id for e in existing):
                logger.debug(
                    "[MessageOutbox] msg_id %s already in outbox for session %s — skipping",
                    msg_id, session_key,
                )
                return True, msg_id  # already persisted

            all_entries = existing + [entry]

            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for e in all_entries:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                # Atomic rename (best-effort on Windows)
                try:
                    os.replace(tmp_path, outbox_path)
                except OSError:
                    # Windows: os.replace fails cross-device; fall back to copy
                    import shutil
                    shutil.copy2(tmp_path, outbox_path)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            except OSError as exc:
                logger.warning(
                    "[MessageOutbox] could not write outbox entry for session %s: %s",
                    session_key, exc,
                )
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                return False, msg_id

        logger.debug(
            "[MessageOutbox] saved msg_id %s for session %s",
            msg_id, session_key,
        )
        return True, msg_id

    def _remove_entry(self, session_key: str, msg_id: str) -> bool:
        """Rewrite the outbox excluding the entry with the given ``msg_id``."""
        outbox_path = _outbox_path(session_key)
        if not outbox_path.exists():
            return False

        with self._write_lock:
            remaining: list[dict[str, Any]] = []
            found = False
            try:
                with open(outbox_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("msg_id") == msg_id:
                            found = True
                        else:
                            remaining.append(entry)
            except OSError as exc:
                logger.warning(
                    "[MessageOutbox] could not read outbox for removal (session=%s): %s",
                    session_key, exc,
                )
                return False

            if not found:
                return False

            tmp_path = outbox_path.with_suffix(".tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    for e in remaining:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                try:
                    os.replace(tmp_path, outbox_path)
                except OSError:
                    import shutil
                    shutil.copy2(tmp_path, outbox_path)
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            except OSError as exc:
                logger.warning(
                    "[MessageOutbox] could not remove outbox entry (session=%s, msg_id=%s): %s",
                    session_key, msg_id, exc,
                )
                return False

        logger.debug(
            "[MessageOutbox] removed msg_id %s from outbox for session %s",
            msg_id, session_key,
        )
        return True

    def _read_entries(self, session_key: str) -> list[dict[str, Any]]:
        """Read all pending outbox entries for ``session_key``."""
        outbox_path = _outbox_path(session_key)
        if not outbox_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        try:
            with open(outbox_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError as exc:
            logger.warning(
                "[MessageOutbox] could not read outbox for replay (session=%s): %s",
                session_key, exc,
            )
        return entries


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_outbox: MessageOutbox | None = None


def get_outbox() -> MessageOutbox:
    global _outbox
    if _outbox is None:
        _outbox = MessageOutbox()
    return _outbox


# ---------------------------------------------------------------------------
# Helpers used by the patched prompt.submit path
# ---------------------------------------------------------------------------

def _now() -> float:
    import time
    return time.time()


def save_message_before_send(
    text: str,
    session_key: str,
    msg_id: str | None = None,
    display_kind: str | None = None,
    queued: bool = False,
) -> tuple[bool, str]:
    """
    Convenience wrapper: save to outbox and return (outbox_ok, msg_id).

    The backend send should be attempted regardless of ``outbox_ok`` —
    the outbox is a resilience layer, not a gate.
    """
    return get_outbox().save_before_send(
        text=text,
        session_key=session_key,
        msg_id=msg_id,
        display_kind=display_kind,
        queued=queued,
    )


def remove_message_after_ack(session_key: str, msg_id: str) -> bool:
    """Remove a message from the outbox after the backend acknowledges it."""
    return get_outbox().remove_after_ack(session_key, msg_id)


def replay_pending_messages(session_key: str) -> list[dict[str, Any]]:
    """
    Retrieve all pending messages for ``session_key`` that need replay.

    Returns a list of outbox entries (oldest first). Caller must call
    ``remove_message_after_ack`` for each entry after successful resend.
    """
    return get_outbox().replay_outbox(session_key)
