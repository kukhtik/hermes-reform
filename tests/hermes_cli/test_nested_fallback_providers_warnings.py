"""Tests for startup warnings when fallback_providers is nested under model:."""

from hermes_cli.fallback_config import _warn_nested_fallback_placement


class TestWarnNestedFallbackPlacement:
    """Test _warn_nested_fallback_placement."""

    def test_nested_no_top_warning_once(self):
        """(a) nested + no top → warning fired once with contract text."""
        config = {
            "model": {
                "provider": "openai",
                "fallback_providers": [{"provider": "anthropic", "model": "claude-3"}],
            }
        }
        warnings = _warn_nested_fallback_placement(config)
        assert len(warnings) == 1
        assert "fallback_providers" in warnings[0]
        assert "model" in warnings[0]
        assert "IGNORED" in warnings[0]

    def test_nested_with_top_warns_top_wins(self):
        """(b) nested + top present → warning that top wins."""
        config = {
            "model": {
                "provider": "openai",
                "fallback_providers": [{"provider": "anthropic", "model": "claude-3"}],
            },
            "fallback_providers": [{"provider": "google", "model": "gemini-2"}],
        }
        warnings = _warn_nested_fallback_placement(config)
        assert len(warnings) == 1
        assert "top-level" in warnings[0] or "top level" in warnings[0].lower()

    def test_neither_nested_nor_top_no_warning(self):
        """(c) neither nested nor top → no warning."""
        config = {"model": {"provider": "openai"}}
        warnings = _warn_nested_fallback_placement(config)
        assert warnings == []

    def test_top_only_no_warning(self):
        """Top-level fallback_providers alone → no warning."""
        config = {
            "fallback_providers": [{"provider": "anthropic", "model": "claude-3"}]
        }
        warnings = _warn_nested_fallback_placement(config)
        assert warnings == []
