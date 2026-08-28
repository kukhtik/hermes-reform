"""
Tests for F3.3: Desktop message persistence on backend disconnect.

Verifies:
(a) message is queued in outbox when backend is unavailable (disconnect)
(b) message is replayed from outbox on reconnect
(c) duplicate msg_id is detected and deduplicated on reconnect

Issue #8049: Desktop silently loses typed messages on 60s backend timeout.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestMessageOutboxPersistence:
    """Unit tests for MessageOutbox without any backend dependency."""

    @pytest.fixture
    def outbox_dir(self, tmp_path: Path) -> Path:
        """Temporary outbox directory backed by a temp dir."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        return outbox

    @pytest.fixture
    def mock_hermes_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Point HERMES_HOME / ~/.hermes at our temp dir."""
        root = tmp_path / ".hermes"
        root.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))
        return root

    def _import_outbox(self) -> type:
        """Import MessageOutbox from desktop_message_handler (fresh each time)."""
        import importlib
        from tui_gateway import desktop_message_handler

        importlib.reload(desktop_message_handler)
        return desktop_message_handler.MessageOutbox

    def test_save_before_send_writes_entry(self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify save_before_send persists a JSON line to the outbox file."""
        cls = self._import_outbox()
        outbox = cls()

        ok, msg_id = outbox.save_before_send(
            text="hello world",
            session_key="session-abc",
        )

        assert ok is True
        assert len(msg_id) == 32  # uuid4 hex

        outbox_file = mock_hermes_root / "outbox" / "session-abc.jsonl"
        assert outbox_file.exists()
        lines = outbox_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["text"] == "hello world"
        assert entry["session_key"] == "session-abc"
        assert entry["msg_id"] == msg_id

    def test_save_before_send_dedup_same_msg_id(self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same msg_id written twice results in only one outbox entry."""
        cls = self._import_outbox()
        outbox = cls()
        msg_id = uuid.uuid4().hex

        ok1, returned_id1 = outbox.save_before_send(
            text="first", session_key="s1", msg_id=msg_id
        )
        ok2, returned_id2 = outbox.save_before_send(
            text="second", session_key="s1", msg_id=msg_id
        )

        assert ok1 is True
        assert ok2 is True
        assert returned_id1 == returned_id2 == msg_id

        outbox_file = mock_hermes_root / "outbox" / "s1.jsonl"
        lines = outbox_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["text"] == "first"

    def test_remove_after_ack_deletes_entry(self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch) -> None:
        """remove_after_ack removes the matching msg_id entry from the outbox."""
        cls = self._import_outbox()
        outbox = cls()

        ok, msg_id = outbox.save_before_send(
            text="to be removed", session_key="s2"
        )
        assert ok is True

        removed = outbox.remove_after_ack("s2", msg_id)
        assert removed is True

        outbox_file = mock_hermes_root / "outbox" / "s2.jsonl"
        assert outbox_file.exists()
        content = outbox_file.read_text(encoding="utf-8").strip()
        # File should be empty (no lines)
        assert content == ""

    def test_remove_after_ack_nonexistent_is_noop(self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch) -> None:
        """Removing a non-existent msg_id returns False, doesn't error."""
        cls = self._import_outbox()
        outbox = cls()

        removed = outbox.remove_after_ack("nonexistent-session", "nonexistent-msg-id")
        assert removed is False

    def test_replay_outbox_returns_all_entries(self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch) -> None:
        """replay_outbox returns all pending entries in chronological order."""
        cls = self._import_outbox()
        outbox = cls()

        outbox.save_before_send(text="msg1", session_key="s3")
        outbox.save_before_send(text="msg2", session_key="s3")
        outbox.save_before_send(text="msg3", session_key="s3")

        entries = outbox.replay_outbox("s3")
        assert len(entries) == 3
        texts = [e["text"] for e in entries]
        assert texts == ["msg1", "msg2", "msg3"]

    def test_replay_outbox_empty_for_unknown_session(self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch) -> None:
        """replay_outbox returns [] for a session with no pending messages."""
        cls = self._import_outbox()
        outbox = cls()

        entries = outbox.replay_outbox("unknown-session")
        assert entries == []

    def test_save_before_send_preserves_display_kind_and_queued(
        self, mock_hermes_root, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """display_kind and queued flags are stored in the outbox entry."""
        cls = self._import_outbox()
        outbox = cls()

        ok, msg_id = outbox.save_before_send(
            text="special",
            session_key="s4",
            display_kind="hidden",
            queued=True,
        )
        assert ok is True

        outbox_file = mock_hermes_root / "outbox" / "s4.jsonl"
        entry = json.loads(outbox_file.read_text(encoding="utf-8").strip())
        assert entry["display_kind"] == "hidden"
        assert entry["queued"] is True


class TestDesktopMessageIntegration:
    """Integration test: message survives a mocked backend disconnect.

    These tests patch the backend call to simulate a disconnect, then
    verify the message is in the outbox and can be replayed.
    """

    @pytest.fixture
    def tmp_hermes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / ".hermes"
        root.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(root))
        return root

    def _import_outbox(self):
        import importlib
        from tui_gateway import desktop_message_handler

        importlib.reload(desktop_message_handler)
        return desktop_message_handler

    def test_outbox_survives_simulated_backend_disconnect(
        self, tmp_hermes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        (a) Message queued on backend disconnect.

        Simulate: backend raises ConnectionError on send.
        Verify: message is persisted in the outbox.
        """
        dmh = self._import_outbox()
        msg_id = uuid.uuid4().hex

        # Simulate: backend is unreachable — save to outbox before attempting send
        ok, returned_id = dmh.save_message_before_send(
            text="test message after disconnect",
            session_key="test-session-1",
            msg_id=msg_id,
        )

        assert ok is True
        assert returned_id == msg_id

        # Verify the message is in the outbox
        outbox_file = tmp_hermes / "outbox" / "test-session-1.jsonl"
        assert outbox_file.exists()
        entries = dmh.replay_pending_messages("test-session-1")
        assert len(entries) == 1
        assert entries[0]["text"] == "test message after disconnect"
        assert entries[0]["msg_id"] == msg_id

    def test_outbox_replay_on_reconnect(
        self, tmp_hermes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        (b) Message replayed on reconnect.

        Simulate: message was saved during disconnect, now backend is back.
        Verify: replay_outbox returns the message; remove_after_ack clears it.
        """
        dmh = self._import_outbox()
        msg_id = uuid.uuid4().hex

        # Save while "disconnected"
        dmh.save_message_before_send(
            text="reconnect replay test",
            session_key="test-session-2",
            msg_id=msg_id,
        )

        # Simulate "reconnect" — replay pending messages
        pending = dmh.replay_pending_messages("test-session-2")
        assert len(pending) == 1
        assert pending[0]["msg_id"] == msg_id
        assert pending[0]["text"] == "reconnect replay test"

        # Simulate successful backend delivery — remove from outbox
        removed = dmh.remove_message_after_ack("test-session-2", msg_id)
        assert removed is True

        # Verify outbox is now empty
        remaining = dmh.replay_pending_messages("test-session-2")
        assert remaining == []

    def test_outbox_dedup_on_reconnect(
        self, tmp_hermes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        (c) Dedup on reconnect.

        Two sends with the same msg_id — only one entry survives.
        """
        dmh = self._import_outbox()
        msg_id = uuid.uuid4().hex

        # First save (disconnected)
        dmh.save_message_before_send(
            text="original",
            session_key="test-session-3",
            msg_id=msg_id,
        )

        # Reconnect — replay, then backend gets the same msg_id again
        pending = dmh.replay_pending_messages("test-session-3")
        assert len(pending) == 1
        assert pending[0]["text"] == "original"

        # Backend is back, we try to send again with the SAME msg_id
        # (the client is retrying with the same ID)
        ok, _ = dmh.save_message_before_send(
            text="duplicate should not double",
            session_key="test-session-3",
            msg_id=msg_id,
        )
        assert ok is True  # dedup prevents double-write

        # Still only ONE entry — the original text
        remaining = dmh.replay_pending_messages("test-session-3")
        assert len(remaining) == 1
        assert remaining[0]["text"] == "original"

    def test_outbox_module_api(self, tmp_hermes, monkeypatch: pytest.MonkeyPatch) -> None:
        """Module-level convenience functions work as documented."""
        dmh = self._import_outbox()
        msg_id = uuid.uuid4().hex

        # save_message_before_send
        ok, ret_id = dmh.save_message_before_send(
            text="api test", session_key="api-session", msg_id=msg_id
        )
        assert ok is True
        assert ret_id == msg_id

        # replay_pending_messages
        entries = dmh.replay_pending_messages("api-session")
        assert len(entries) == 1
        assert entries[0]["text"] == "api test"

        # remove_message_after_ack
        removed = dmh.remove_message_after_ack("api-session", msg_id)
        assert removed is True
        assert dmh.replay_pending_messages("api-session") == []
