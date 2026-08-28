"""
Tests for delegation.allow_sync_fallback default = false.

Issue: #52868 — delegate_task sync fallback creates unbounded sessions.
Task F0.3 contract:
  (a) default is false
  (b) explicit true enables fallback
  (c) false + saturation → raises hard error with clear message
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


class TestAllowSyncFallbackDefault:
    """Tests for delegation.allow_sync_fallback config key."""

    def test_default_is_false(self):
        """
        DEFAULT_CONFIG['delegation']['allow_sync_fallback'] must be False.
        """
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        delegation_cfg = DEFAULT_CONFIG.get("delegation", {})
        assert (
            "allow_sync_fallback" in delegation_cfg
        ), "allow_sync_fallback key must exist in delegation config"
        assert (
            delegation_cfg["allow_sync_fallback"] is False
        ), "default allow_sync_fallback must be False"

    def test_false_plus_saturation_raises_DelegationError(self):
        """
        allow_sync_fallback=false AND async pool saturated → raises DelegationError
        with a clear message instead of silently falling back to sync.
        """
        import tools.async_delegation as async_delegation
        import tools.delegate_tool as delegate_tool

        mock_cfg = {"allow_sync_fallback": False, "max_concurrent_children": 1}

        # Build a real child the way test_delegate_cron_sync_fallback.py does
        def _make_child(**kwargs):
            child = MagicMock()
            child.model = "test-model"
            child._delegate_depth = 1
            child._delegate_role = "leaf"
            child._subagent_id = "test-subagent"
            child._delegate_saved_tool_names = []
            child._credential_pool = None
            return child

        def _mock_credentials(*a, **k):
            return {
                "model": "test-model",
                "provider": None,
                "base_url": None,
                "api_key": None,
                "api_mode": None,
                "command": None,
                "args": None,
            }

        parent_agent = MagicMock()
        parent_agent.model = "test-model"
        parent_agent._delegate_spinner = None
        parent_agent._interrupt_requested = False
        parent_agent._active_children = []
        from threading import RLock
        parent_agent._active_children_lock = RLock()
        parent_agent.session_id = "test-session"
        parent_agent._delegate_depth = 0
        parent_agent._delegate_role = "leaf"
        parent_agent._credential_pool = None

        with patch.object(delegate_tool, "_load_config", return_value=mock_cfg):
            with patch.object(delegate_tool, "_build_child_agent", side_effect=_make_child):
                with patch.object(delegate_tool, "_resolve_delegation_credentials", side_effect=_mock_credentials):
                    with patch.object(
                        async_delegation,
                        "dispatch_async_delegation_batch",
                        return_value={"error": "Async delegation capacity reached (1 max)"},
                    ):
                        with patch(
                            "gateway.session_context.async_delivery_supported",
                            return_value=True,
                        ):
                            with patch(
                                "gateway.session_context.get_session_env",
                                return_value="",
                            ):
                                with patch(
                                    "tools.approval.get_current_session_key",
                                    return_value="test-session",
                                ):
                                    with pytest.raises(delegate_tool.DelegationError) as exc_info:
                                        delegate_tool.delegate_task(
                                            goal="test goal",
                                            parent_agent=parent_agent,
                                            background=True,
                                        )

                                    exc_msg = str(exc_info.value)
                                    assert "allow_sync_fallback=false" in exc_msg, (
                                        f"Error must mention allow_sync_fallback=false: {exc_msg}"
                                    )
                                    assert any(
                                        kw in exc_msg.lower()
                                        for kw in ("saturated", "capacity", "pool")
                                    ), f"Error must mention pool saturation: {exc_msg}"

    def test_true_uses_sync_fallback_path(self):
        """
        allow_sync_fallback=true → pool-saturation does NOT raise DelegationError;
        sync fallback is attempted (may fail for other reasons but not DelegationError).
        """
        import tools.async_delegation as async_delegation
        import tools.delegate_tool as delegate_tool

        mock_cfg = {"allow_sync_fallback": True, "max_concurrent_children": 1}

        def _make_child(**kwargs):
            child = MagicMock()
            child.model = "test-model"
            child._delegate_depth = 1
            child._delegate_role = "leaf"
            child._subagent_id = "test-subagent"
            child._delegate_saved_tool_names = []
            child._credential_pool = None
            return child

        def _mock_credentials(*a, **k):
            return {
                "model": "test-model",
                "provider": None,
                "base_url": None,
                "api_key": None,
                "api_mode": None,
                "command": None,
                "args": None,
            }

        # Mock _run_single_child to avoid MagicMock non-serializability
        # (nested _execute_and_aggregate calls it with a MagicMock child;
        # patching the nested function is impossible, so we patch the inner
        # callable it calls).
        def _mock_run_single_child(task_index, goal, child, parent_agent, **kwargs):
            return {"role": "assistant", "content": "mock child result"}

        parent_agent = MagicMock()
        parent_agent.model = "test-model"
        parent_agent._delegate_spinner = None
        parent_agent._interrupt_requested = False
        parent_agent._active_children = []
        from threading import RLock
        parent_agent._active_children_lock = RLock()
        parent_agent.session_id = "test-session"
        parent_agent._delegate_depth = 0
        parent_agent._delegate_role = "leaf"
        parent_agent._credential_pool = None

        with patch.object(delegate_tool, "_load_config", return_value=mock_cfg):
            with patch.object(delegate_tool, "_build_child_agent", side_effect=_make_child):
                with patch.object(delegate_tool, "_resolve_delegation_credentials", side_effect=_mock_credentials):
                    with patch.object(
                        async_delegation,
                        "dispatch_async_delegation_batch",
                        return_value={"error": "Async delegation capacity reached (1 max)"},
                    ):
                        with patch(
                            "gateway.session_context.async_delivery_supported",
                            return_value=True,
                        ):
                            with patch(
                                "gateway.session_context.get_session_env",
                                return_value="",
                            ):
                                with patch(
                                    "tools.approval.get_current_session_key",
                                    return_value="test-session",
                                ):
                                    with patch.object(
                                        delegate_tool,
                                        "_run_single_child",
                                        side_effect=_mock_run_single_child,
                                    ):
                                        # Should NOT raise DelegationError when allow_sync_fallback=True
                                        try:
                                            delegate_tool.delegate_task(
                                                goal="test goal",
                                                parent_agent=parent_agent,
                                                background=True,
                                            )
                                        except delegate_tool.DelegationError:
                                            pytest.fail(
                                                "DelegationError should NOT be raised when "
                                                "allow_sync_fallback=True"
                                            )
                                        # Other exceptions (e.g. from the sync fallback execution)
                                        # are acceptable — the guard did not block the path
