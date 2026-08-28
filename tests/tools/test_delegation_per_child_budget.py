#!/usr/bin/env python3
"""Per-delegation budget: max_child_tokens_total / max_child_api_calls_total.

Tests:
1. Config defaults exist and are 5_000_000 tokens / 500 api calls
2. Batch is rejected with clear error when projected budget is exceeded
3. Counters are per-session readable
"""

import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import delegate_task, _load_config


def _make_mock_parent(depth=0, session_id="test-session-1"):
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._session_id = session_id
    return parent


class TestBudgetConfigDefaults(unittest.TestCase):
    """AC1: config keys exist with correct defaults."""

    def test_max_child_tokens_total_default(self):
        cfg = _load_config()
        self.assertEqual(cfg.get("max_child_tokens_total"), 5_000_000)

    def test_max_child_api_calls_total_default(self):
        cfg = _load_config()
        self.assertEqual(cfg.get("max_child_api_calls_total"), 500)


class TestBudgetEnforcement(unittest.TestCase):
    """AC2: batch rejected with clear error when projected budget exceeded."""

    def test_batch_rejected_when_token_budget_exceeded(self):
        """When projected tokens exceed max_child_tokens_total, reject batch."""
        parent = _make_mock_parent()
        # Build a batch where each task has an estimated token cost
        # We mock _check_budget to simulate over-budget condition
        with patch("tools.delegate_tool._check_session_budget") as mock_check:
            mock_check.return_value = (
                False,
                "max_child_tokens_total exceeded: "
                "consumed 4_900_000 / 5_000_000, "
                "batch requests 200_000 more",
            )
            result = delegate_task(
                tasks=[
                    {"goal": "Task A that needs many tokens to process"},
                    {"goal": "Task B that also requires substantial context"},
                ],
                parent_agent=parent,
            )
        parsed = json.loads(result)
        self.assertEqual(parsed.get("code"), "BUDGET_EXCEEDED")
        self.assertIn("max_child_tokens_total", parsed.get("detail", ""))

    def test_batch_rejected_when_api_calls_budget_exceeded(self):
        """When projected API calls exceed max_child_api_calls_total, reject."""
        parent = _make_mock_parent()
        with patch("tools.delegate_tool._check_session_budget") as mock_check:
            mock_check.return_value = (
                False,
                "max_child_api_calls_total exceeded: "
                "consumed 490 / 500, batch requests 20 more",
            )
            result = delegate_task(
                tasks=[
                    {"goal": "Task that makes many API calls"},
                ],
                parent_agent=parent,
            )
        parsed = json.loads(result)
        self.assertEqual(parsed.get("code"), "BUDGET_EXCEEDED")
        self.assertIn("max_child_api_calls_total", parsed.get("detail", ""))

    def test_batch_allowed_when_within_budget(self):
        """When projected budget is within limits, batch proceeds."""
        from tools.delegate_tool import _check_session_budget
        # A single task: est_tokens=100_000, est_api=10 — both under 5M/500 defaults
        allowed, detail = _check_session_budget(
            "test-session", batch_size=1, estimated_tokens=100_000, estimated_api_calls=10
        )
        self.assertTrue(allowed)
        self.assertEqual(detail, "")


class TestBudgetCounters(unittest.TestCase):
    """AC3: counters readable per-session."""

    def test_counters_per_session_isolated(self):
        """Counters for different sessions are isolated."""
        from tools.delegate_tool import (
            _increment_session_counters,
            _get_session_counters,
            _reset_session_counters,
        )
        _reset_session_counters("session-a")
        _reset_session_counters("session-b")

        _increment_session_counters("session-a", tokens=1000, api_calls=5)
        _increment_session_counters("session-b", tokens=500, api_calls=2)

        counters_a = _get_session_counters("session-a")
        counters_b = _get_session_counters("session-b")

        self.assertEqual(counters_a["tokens"], 1000)
        self.assertEqual(counters_a["api_calls"], 5)
        self.assertEqual(counters_b["tokens"], 500)
        self.assertEqual(counters_b["api_calls"], 2)

    def test_counters_readable_via_status(self):
        """Session counters are readable via delegation status."""
        from tools.delegate_tool import (
            _increment_session_counters,
            _get_session_counters,
            _reset_session_counters,
        )
        import tools.delegate_tool as dt

        _reset_session_counters("status-test-session")
        _increment_session_counters("status-test-session", tokens=42_000, api_calls=13)

        # Simulate what hermes delegation status would surface
        counters = _get_session_counters("status-test-session")
        self.assertEqual(counters["tokens"], 42_000)
        self.assertEqual(counters["api_calls"], 13)


if __name__ == "__main__":
    unittest.main()
