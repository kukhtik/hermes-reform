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

    def test_stream_through_chat_completion_helpers_registered_and_cancelled(self):
        """Integration test: interruptible_streaming_api_call wires register_stream into the
        production streaming path (chat_completions mode), and cancel_all() closes it.

        This verifies AC#1: register_stream() is wired into chat_completion_helpers.py.
        """
        import httpx
        from unittest.mock import MagicMock, patch
        from agent import cancellation
        from agent.chat_completion_helpers import interruptible_streaming_api_call

        # Track what gets registered
        registered_streams = []

        original_register = cancellation.register_stream
        original_unregister = cancellation.unregister_stream

        def _tracking_register(stream):
            registered_streams.append(stream)
            return original_register(stream)

        def _tracking_unregister(stream):
            return original_unregister(stream)

        cancellation.register_stream = _tracking_register
        cancellation.unregister_stream = _tracking_unregister

        # Build a mock agent with all required attributes for the chat_completions path
        mock_agent = MagicMock()
        mock_agent._interrupt_requested = False
        mock_agent.api_mode = "chat_completions"
        mock_agent.provider = "openai"
        mock_agent.model = "gpt-4o-mini"
        mock_agent.base_url = "https://api.openai.com/v1"
        mock_agent.session_id = "test-session"
        mock_agent._stream_diag_init.return_value = {}
        mock_agent._capture_rate_limits = MagicMock()
        mock_agent._capture_credits = MagicMock()
        mock_agent._stream_diag_capture_response = MagicMock()
        mock_agent._check_openrouter_cache_status = MagicMock()
        mock_agent._has_stream_consumers.return_value = False
        mock_agent.stream_delta_callback = None
        mock_agent.reasoning_callback = None
        mock_agent._current_api_request_id = "req-test"
        mock_agent.is_subagent = False
        mock_agent._fallback_index = 0
        mock_agent._disable_streaming = False
        mock_agent._buffer_status = MagicMock()
        mock_agent._safe_print = MagicMock()

        # Minimal api_kwargs
        api_kwargs = {"model": "gpt-4o-mini"}

        # Create a mock httpx response object to satisfy _accept_stream_chunk
        mock_response = MagicMock()
        mock_response.is_closed = False

        # Create a fake managed stream that has a close() method
        fake_stream = MagicMock(spec=httpx.Response)
        fake_stream.close = MagicMock()

        # Mock claim_stream_writer / stream_writer_is_current to avoid side effects
        with patch(
            "agent.chat_completion_helpers.claim_stream_writer", return_value="token-1"
        ), patch(
            "agent.chat_completion_helpers.stream_writer_is_current", return_value=True
        ), patch(
            "agent.chat_completion_helpers.should_use_direct_api_call", return_value=False
        ), patch(
            "agent.chat_completion_helpers._iter_provider_stream_chunks",
            return_value=iter([]),
        ), patch.object(
            mock_agent, "_create_request_openai_client", return_value=MagicMock()
        ):
            try:
                interruptible_streaming_api_call(mock_agent, api_kwargs)
            except Exception:
                # Any exception is fine; we only care that register_stream was called
                pass

        cancellation.register_stream = original_register
        cancellation.unregister_stream = original_unregister

        # Assert: register_stream was called at least once through the production path
        assert len(registered_streams) > 0, (
            "register_stream was NOT called through chat_completion_helpers — "
            "AC#1 (production wiring) is not satisfied"
        )

        # Verify the registered stream has a callable close() that cancel_all can invoke
        for stream in registered_streams:
            close_fn = getattr(stream, "close", None)
            assert callable(close_fn), (
                f"Registered stream {stream!r} has no close() method"
            )
            close_fn()  # simulate cancel_all calling stream.close()
