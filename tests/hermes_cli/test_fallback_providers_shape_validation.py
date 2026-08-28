"""Tests for fallback_providers shape validation at config-set time (issue #51560).

When ``hermes config set fallback_providers <value>`` is called, the shape
of the value is validated BEFORE it is written to config.yaml:

(a) A JSON string is parsed and stored as a list.
(b) An invalid string is rejected with non-zero exit and config is not modified.
(c) A single dict is wrapped in a list.
(d) An empty list is allowed but emits a warning.
"""

from __future__ import annotations

import sys
import pytest
from unittest import mock


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _set_via_set_config_value(key: str, value: str, monkeypatch, tmp_path):
    """Call set_config_value and capture exit/exceptions."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Ensure no real config exists so we don't depend on external state
    (tmp_path / "config.yaml").write_text("")
    from hermes_cli import config as cfg
    try:
        cfg.set_config_value(key, value)
        return None
    except SystemExit as e:
        return e.code


def _read_fallback_providers(tmp_path):
    """Read fallback_providers from the config file on disk."""
    import yaml

    cfg_path = tmp_path / "config.yaml"
    if not cfg_path.exists():
        return None
    data = yaml.safe_load(cfg_path.read_text()) or {}
    return data.get("fallback_providers")


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------


class TestFallbackProvidersSetValidation:
    """Validate fallback_providers at set_config_value time."""

    def test_string_json_parsed_and_list_stored(self, monkeypatch, tmp_path):
        """(a) A string that is valid JSON list → parsed and stored as a list."""
        json_str = '[{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]'
        code = _set_via_set_config_value("fallback_providers", json_str, monkeypatch, tmp_path)
        # Should succeed (exit 0 / None)
        assert code is None, f"Expected success, got exit {code}"
        stored = _read_fallback_providers(tmp_path)
        assert stored == [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]
        assert isinstance(stored, list)

    def test_invalid_string_rejected_config_unchanged(self, monkeypatch, tmp_path):
        """(b) Invalid JSON string → non-zero exit + config NOT modified."""
        # Start with a known config
        import yaml

        cfg_path = tmp_path / "config.yaml"
        original = {"model": {"provider": "openai"}, "fallback_providers": [{"provider": "existing", "model": "gpt-4"}]}
        cfg_path.write_text(yaml.safe_dump(original))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        from hermes_cli import config as cfg

        code = None
        try:
            cfg.set_config_value("fallback_providers", "not valid json at all")
        except SystemExit as e:
            code = e.code

        # Must reject with non-zero
        assert code is not None and code != 0, "Expected non-zero exit for invalid value"
        # Config must be unchanged
        current = yaml.safe_load(cfg_path.read_text()) or {}
        assert current.get("fallback_providers") == original["fallback_providers"]

    def test_single_dict_wrapped_in_list(self, monkeypatch, tmp_path):
        """(c) A single dict is accepted and wrapped in a list."""
        # Single dict passed as YAML/JSON
        dict_str = '{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}'
        code = _set_via_set_config_value("fallback_providers", dict_str, monkeypatch, tmp_path)
        assert code is None, f"Expected success, got exit {code}"
        stored = _read_fallback_providers(tmp_path)
        assert stored == [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4"}]
        assert isinstance(stored, list)

    def test_empty_list_allowed_with_warning(self, monkeypatch, tmp_path, capsys):
        """(d) Empty list is allowed but emits a warning."""
        code = _set_via_set_config_value("fallback_providers", "[]", monkeypatch, tmp_path)
        assert code is None, f"Expected success, got exit {code}"
        stored = _read_fallback_providers(tmp_path)
        assert stored == []
        # Must have warned about empty fallback
        captured = capsys.readouterr()
        assert "WARN" in captured.out or "warn" in captured.out.lower() or "empty" in captured.out.lower()
