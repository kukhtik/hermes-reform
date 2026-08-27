"""Tests for httpx stream cancellation on /stop signal.

Tests:
(a) long-running stream cancelled on stop
(b) cancellation propagates to subagent requests
(c) cancellation is idempotent (multiple /stop safe)
"""

from __future__ import annotations

import threading
import asyncio
from unittest.mock import MagicMock, patch

import pytest


class TestHttpxCancelOnStop:
    """Test suite for F0.5: post-stop HTTP cancellation via httpx cancel."""

    def test_long_running_stream_cancelled_on_stop(self):
        """Test that a long-running streaming HTTP request is cancelled on stop signal."""
        from agent import cancellation

        # Ensure the module is loaded
        assert hasattr(cancellation, "register_stream")
        assert hasattr(cancellation, "unregister_stream")
        assert hasattr(cancellation, "cancel_all")

        # Simulate an httpx stream with close() method
        mock_stream = MagicMock()
        close_called = {"sync": False}

        def _sync_close():
            close_called["sync"] = True

        mock_stream.close = _sync_close
        mock_stream.response = MagicMock()

        # Register stream
        cancellation.register_stream(mock_stream)

        try:
            # Trigger cancel_all (simulates /stop)
            cancellation.cancel_all()

            # Verify close was called on the stream
            assert close_called["sync"], (
                "Stream close() was not called on cancel_all()"
            )
        finally:
            cancellation.unregister_stream(mock_stream)

    def test_cancellation_propagates_to_subagent_requests(self):
        """Test that cancellation propagates to subagent in-flight streams."""
        from agent import cancellation

        parent_stream = MagicMock()
        subagent_stream = MagicMock()
        close_order = []

        def _parent_close():
            close_order.append("parent")

        def _subagent_close():
            close_order.append("subagent")

        parent_stream.close = _parent_close
        parent_stream.response = MagicMock()

        subagent_stream.close = _subagent_close
        subagent_stream.response = MagicMock()

        # Register both parent and subagent streams
        cancellation.register_stream(parent_stream)
        cancellation.register_stream(subagent_stream)

        try:
            # Cancel all (simulates /stop propagation)
            cancellation.cancel_all()

            # Both streams should be closed
            assert "parent" in close_order, "Parent stream was not closed"
            assert "subagent" in close_order, "Subagent stream was not closed"
        finally:
            cancellation.unregister_stream(parent_stream)
            cancellation.unregister_stream(subagent_stream)

    def test_cancellation_is_idempotent(self):
        """Test that cancel_all() is safe to call multiple times (idempotency)."""
        from agent import cancellation

        mock_stream = MagicMock()
        close_count = {"sync": 0}

        def _sync_close():
            close_count["sync"] += 1

        mock_stream.close = _sync_close
        mock_stream.response = MagicMock()

        cancellation.register_stream(mock_stream)

        try:
            # Call cancel_all multiple times (simulates multiple /stop)
            cancellation.cancel_all()
            cancellation.cancel_all()
            cancellation.cancel_all()

            # After first cancel_all, the stream is unregistered.
            # Subsequent calls should be no-ops (not crash).
            cancellation.cancel_all()  # Should not raise
        finally:
            # Idempotent unregister is safe
            cancellation.unregister_stream(mock_stream)
            cancellation.unregister_stream(mock_stream)

    def test_aclose_fallback_when_no_sync_close(self):
        """Verify aclose() fallback is used when only async close exists."""
        from agent import cancellation

        mock_stream = MagicMock()
        close_called = {"async": False}

        # Stream with only aclose (no sync close)
        async def _async_close():
            close_called["async"] = True

        mock_stream.aclose = _async_close
        mock_stream.response = MagicMock()
        # Remove sync close to test fallback
        del mock_stream.close

        cancellation.register_stream(mock_stream)

        try:
            # cancel_all should fall back to aclose in async context
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(cancellation.cancel_all_async())
            finally:
                loop.close()

            assert close_called["async"], (
                "Stream aclose() was not called when sync close unavailable"
            )
        finally:
            cancellation.unregister_stream(mock_stream)

    def test_unregister_removes_from_cancel_all(self):
        """Test that unregister prevents a stream from being cancelled."""
        from agent import cancellation

        mock_stream = MagicMock()
        close_called = {"sync": False}

        def _sync_close():
            close_called["sync"] = True

        mock_stream.close = _sync_close
        mock_stream.response = MagicMock()

        cancellation.register_stream(mock_stream)
        cancellation.unregister_stream(mock_stream)

        # cancel_all should not call close on unregistered stream
        cancellation.cancel_all()

        assert not close_called["sync"], (
            "Unregistered stream should not be closed"
        )
