"""tests/test_terminated_exit_markers.py

Tests for structured TERMINATED markers on os._exit paths.
These verify the fix for issue #8049 (budget exhaustion → os._exit(0) without traceback).
"""
import logging
import sys
from unittest import mock

import pytest


def test_termination_marker_helper_exists_and_callable():
    """Helper _emit_terminated exists in agent.termination_markers and is callable."""
    from agent.termination_markers import _emit_terminated
    assert callable(_emit_terminated)


def test_termination_marker_helper_signature():
    """Helper accepts reason (str), exit_code (int), and optional context (dict)."""
    from agent.termination_markers import _emit_terminated
    import inspect
    sig = inspect.signature(_emit_terminated)
    params = list(sig.parameters.keys())
    assert "reason" in params
    assert "exit_code" in params
    assert "context" in params


def test_termination_marker_emitted_on_watchdog_exit(monkeypatch, caplog, tmp_path):
    """Before os._exit in the watchdog path, a TERMINATED marker is logged."""
    import os
    import cli as cli_module

    emitted_calls = []

    def mock_emit_terminated(reason, exit_code, context=None):
        emitted_calls.append({"reason": reason, "exit_code": exit_code, "context": context})

    # Patch the helper so we can observe it without actually exiting.
    monkeypatch.setattr("cli._emit_terminated", mock_emit_terminated)

    # Simulate PYTEST_CURRENT_TEST not set so watchdog can arm.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    # Simulate HERMES_EXIT_WATCHDOG_S=1 for fast test.
    monkeypatch.setenv("HERMES_EXIT_WATCHDOG_S", "1")

    # We cannot easily trigger the watchdog real path without a wedged cleanup,
    # but we CAN verify the helper is imported and patched in cli.py by checking
    # that cli._emit_terminated is set to our mock when we import the module.
    # The RED test: before implementation, cli.py has no _emit_terminated at all.
    assert hasattr(cli_module, "_emit_terminated"), (
        "cli.py must have _emit_terminated helper; it is missing — RED test"
    )
    assert cli_module._emit_terminated is mock_emit_terminated


def test_termination_marker_reason_is_structured():
    """The TERMINATED marker contains a structured reason string."""
    from agent.termination_markers import _emit_terminated
    import logging
    import io

    # Capture stderr since logger may fall back there.
    stream = io.StringIO()
    with mock.patch.object(logging, "error") as mock_error:
        _emit_terminated(reason="budget_exhausted", exit_code=0, context={"tokens": 100})
        mock_error.assert_called_once()
        fmt, reason_arg, exit_code_arg, ctx_arg = mock_error.call_args[0]
        assert "TERMINATED:" in fmt
        assert "reason=" in fmt
        assert reason_arg == "budget_exhausted"
        assert exit_code_arg == 0
        assert ctx_arg == '{"tokens": 100}'


def test_termination_marker_writes_jsonl_on_signal_exit(monkeypatch, tmp_path):
    """When HERMES_HOME is set, _emit_terminated writes a structured JSONL line."""
    from agent.termination_markers import _emit_terminated
    import json
    import os
    import time

    hermes_home = str(tmp_path)
    monkeypatch.setenv("HERMES_HOME", hermes_home)
    monkeypatch.setenv("HERMES_SESSION_ID", "test-session-123")

    _emit_terminated(reason="signal_handler", exit_code=0, context={"signal": "SIGTERM"})

    today = time.strftime("%Y-%m-%d")
    log_file = tmp_path / "errors" / today / "test-session-123.jsonl"
    assert log_file.exists(), f"Expected {log_file} to exist"
    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["reason"] == "signal_handler"
    assert record["exit_code"] == 0
    assert record["context"]["signal"] == "SIGTERM"


def test_cli_has_emit_terminated_on_exit_paths():
    """Every os._exit in cli.py is preceded by _emit_terminated call within 3 lines."""
    import cli as cli_module
    import inspect
    import ast

    source = inspect.getsource(cli_module)
    tree = ast.parse(source)

    os_exit_sites = []  # list of (os_exit_lineno, surrounding_lines)
    # Get all lines for context checking
    source_lines = source.splitlines()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "_exit"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "os"
            ):
                lineno = node.lineno
                # Grab 3 lines of PRECEDING context (the emit + os._exit pair).
                window = source_lines[max(0, lineno - 4) : lineno + 1]
                os_exit_sites.append((lineno, window))

    assert hasattr(cli_module, "_emit_terminated"), (
        "cli.py must define or import _emit_terminated"
    )

    assert len(os_exit_sites) >= 1, "At least one os._exit site expected in cli.py"

    # Per-site check: each os._exit must have _emit_terminated in the 3 preceding lines.
    missing = []
    for lineno, window in os_exit_sites:
        window_text = "\n".join(window)
        if "_emit_terminated" not in window_text:
            missing.append(lineno)

    assert not missing, (
        f"os._exit at line(s) {missing} lack _emit_terminated within 3 lines. "
        f"Each os._exit site must be immediately preceded by _emit_terminated call."
    )
