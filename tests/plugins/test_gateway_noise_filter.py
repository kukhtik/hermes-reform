"""Behavioral tests for the gateway noise filter plugin."""

import importlib.util
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_plugin():
    """Import the plugin's __init__.py using PluginManager's naming convention."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_dir = repo_root / "plugins" / "gateway-noise-filter"
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.gateway_noise_filter",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.gateway_noise_filter"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.gateway_noise_filter"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_plugin()


def _event(text, user_id="u1"):
    return SimpleNamespace(text=text, user_id=user_id)


class TestObserveOnlyByDefault:
    """With no config, the filter classifies but never alters dispatch."""

    def test_returns_none_for_machine_notice(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_config", lambda: {})
        assert mod.on_pre_gateway_dispatch(
            event=_event("Background process proc_1 completed")) is None

    def test_returns_none_for_human_message(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_config", lambda: {})
        assert mod.on_pre_gateway_dispatch(event=_event("please run the tests")) is None

    def test_empty_text_is_ignored(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_config", lambda: {})
        assert mod.on_pre_gateway_dispatch(event=_event("   ")) is None

    def test_missing_event_does_not_raise(self, mod):
        assert mod.on_pre_gateway_dispatch() is None


class TestDropMode:
    """When drop is enabled only the configured kinds are skipped."""

    def test_drops_configured_kind(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod, "_config",
            lambda: {"drop": True, "drop_kinds": ["system_notification"]})
        result = mod.on_pre_gateway_dispatch(
            event=_event("Background process proc_1 ok"))
        assert isinstance(result, dict)
        assert result["action"] == "skip"
        assert "system_notification" in result["reason"]

    def test_does_not_drop_human_message_in_drop_mode(self, mod, monkeypatch):
        monkeypatch.setattr(
            mod, "_config",
            lambda: {"drop": True, "drop_kinds": ["system_notification"]})
        assert mod.on_pre_gateway_dispatch(
            event=_event("hello, are you there?")) is None

    def test_drop_kinds_defaults_when_missing(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_config", lambda: {"drop": True})
        result = mod.on_pre_gateway_dispatch(
            event=_event("Background process proc_9 done"))
        assert result and result["action"] == "skip"


class TestFailOpen:
    """A broken classifier must never block dispatch."""

    def test_classifier_exception_returns_none(self, mod, monkeypatch):
        def boom(text):
            raise RuntimeError("classifier broke")

        monkeypatch.setattr(mod, "_classify", boom)
        assert mod.on_pre_gateway_dispatch(event=_event("anything")) is None


class TestRegistration:
    """The plugin registers exactly the hook it needs."""

    def test_registers_pre_gateway_dispatch(self, mod):
        registered = []

        class Ctx:
            def register_hook(self, name, fn):
                registered.append(name)

        mod.register(Ctx())
        assert registered == ["pre_gateway_dispatch"]


class TestLogging:
    """Every classified message is logged when logging is on."""

    def test_logs_classification(self, mod, monkeypatch, caplog):
        monkeypatch.setattr(mod, "_config", lambda: {"log": True})
        with caplog.at_level(logging.INFO):
            mod.on_pre_gateway_dispatch(event=_event("Background process proc_1 ok"))
        assert any("gateway-noise-filter" in r.message for r in caplog.records)
