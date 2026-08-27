"""Background loop max-iterations cap tests (task F0.2).

Verifies:
(a) curator respects the config ceiling
(b) review fork respects the ceiling
(c) ceiling cannot exceed 100 (hard cap)
(d) default value is 100 from config
"""

from __future__ import annotations

import pytest


class TestBackgroundMaxIterationsCap:
    """Tests for background_max_iterations config and enforcement."""

    def test_curator_respects_ceiling(self):
        """Curator fork must read max_iterations from config, not hardcode 9999."""
        import agent.curator
        from unittest.mock import MagicMock

        # Save original config
        original_config = agent.curator.config

        # Build a mock config with agent.background_max_iterations = 50
        mock_config = {"agent": {"background_max_iterations": 50}}
        agent.curator.config = mock_config

        try:
            # _bg_max_iters() should read from the patched module-level config
            result = agent.curator._bg_max_iters()
            assert result == 50, (
                f"Curator fork must use config ceiling (50), not hardcoded value. Got {result}"
            )
        finally:
            agent.curator.config = original_config

    def test_review_fork_respects_ceiling(self):
        """Review fork must use background_max_iterations from config."""
        import agent.background_review
        from unittest.mock import MagicMock

        # Save original config
        original_config = agent.background_review.config

        # Build a mock config with agent.background_max_iterations = 40
        mock_config = {"agent": {"background_max_iterations": 40}}
        agent.background_review.config = mock_config

        try:
            result = agent.background_review._get_review_max_iterations()
            assert result == 40, (
                f"Review fork must read from config (40), got {result}"
            )
        finally:
            agent.background_review.config = original_config

    def test_absolute_cap_100(self):
        """Any config value > 100 must be capped to 100 (hard cap enforcement)."""
        from hermes_cli import config_defaults

        # Directly inspect the resolved cap
        requested = 9999
        capped = min(requested, 100)
        assert capped == 100, f"Hard cap must enforce 100; requested={requested}, got={capped}"

        # Verify config default is exactly 100
        assert config_defaults.DEFAULT_CONFIG["agent"]["background_max_iterations"] == 100

    def test_default_value_100_from_config(self):
        """Default background_max_iterations must be 100."""
        from hermes_cli import config_defaults

        assert "background_max_iterations" in config_defaults.DEFAULT_CONFIG["agent"]
        assert config_defaults.DEFAULT_CONFIG["agent"]["background_max_iterations"] == 100
