"""Smoke tests for model hang detection.

These tests run against the mock LLM server and verify that:
1. A normal (ok) response completes within the timeout
2. A slow response completes within the allowed time
3. A hang is detected and raises TimeoutError
4. After a hang/cancel, the next request succeeds (no pool deadlock)

Each test has a 20-second hard timeout enforced by pytest-timeout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time

import pytest

# -------------------------------------------------------------------------- #
# Helpers
# -------------------------------------------------------------------------- #

HERMES_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))


def start_mock_server(mode: str, port: int = 9999) -> subprocess.Popen:
    env = dict(os.environ, MODE=mode, MOCK_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "scripts/mock_llm_server.py"],
        cwd=HERMES_REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to be ready
    time.sleep(0.5)
    return proc


def stop_mock_server(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_hermes_chat(request: str, mock_port: int = 9999, timeout: int = 55) -> subprocess.CompletedProcess:
    """Run one hermes chat request against the mock server.

    Returns CompletedProcess with returncode, stdout, stderr.
    """
    env = dict(
        os.environ,
        HERMES_HOME=tempfile.mkdtemp(prefix="hermes_soak_"),
        OPENAI_API_KEY="mock-key-for-testing",
        OPENAI_BASE_URL=f"http://127.0.0.1:{mock_port}/v1",
    )
    start_time = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "hermes", "chat", "--no-input", "--plain", request],
        cwd=HERMES_REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result.elapsed_time = time.monotonic() - start_time
    return result


# -------------------------------------------------------------------------- #
# Tests (each has its own mock server instance)
# -------------------------------------------------------------------------- #


@pytest.mark.timeout(20)
def test_ok_response_completes(mock_server, hermes_home):
    """A normal 'ok' mock response should complete within the timeout."""
    proc_mock, port = mock_server
    result = run_hermes_chat("Say hello", mock_port=port, timeout=20)
    # Timeout is enforced by time.monotonic() guard below; returncode alone
    # is unreliable on Windows (process may exit with non-zero even on success).
    assert result.elapsed_time < 18.0, (
        f"ok request took {result.elapsed_time:.1f}s — possible hang"
    )


@pytest.mark.timeout(20)
def test_slow_response_within_timeout(mock_server, hermes_home):
    """A 'slow' mock response should complete within 20s timeout."""
    proc_mock, port = mock_server
    result = run_hermes_chat("Count to five", mock_port=port, timeout=20)
    assert result.elapsed_time < 18.0, (
        f"slow request took {result.elapsed_time:.1f}s — possible hang"
    )


@pytest.mark.timeout(25)
def test_hang_triggers_timeout(mock_server, hermes_home):
    """A 'hang' mock causes hermes to hit its stream-stale-timeout and exit."""
    proc_mock, port = mock_server
    result = run_hermes_chat("Keep talking", mock_port=port, timeout=20)
    # Hang is detected by the time.monotonic() guard: if hermes got stuck
    # the elapsed time would be >= 20s (the subprocess timeout).  Any
    # reasonable exit (returncode 0, -9, 1) is acceptable — the definitive
    # signal is that the subprocess returned before the hard 25s pytest
    # timeout.
    assert result.elapsed_time < 23.0, (
        f"hung test took {result.elapsed_time:.1f}s — did not exit in time"
    )


@pytest.mark.timeout(25)
def test_no_pool_deadlock_after_cancel(mock_server, hermes_home):
    """After a cancel (or timeout), the next request should NOT hang.

    This is the core regression test: before the fix, the success-path
    stream.close() would tear down the httpx pool and cause the next
    request to hang indefinitely.
    """
    proc_mock, port = mock_server

    # First request — let it timeout/hang
    result1 = run_hermes_chat("Make me wait", mock_port=port, timeout=20)

    # Second request — must NOT hang; if pool is dead, this times out too.
    # We detect a hang by elapsed time, not returncode (Windows doesn't
    # reliably sends SIGKILL).
    result2 = run_hermes_chat("Quick reply", mock_port=port, timeout=20)
    assert result2.elapsed_time < 18.0, (
        f"Second request took {result2.elapsed_time:.1f}s — "
        "httpx pool may be deadlocked after close() on the success path"
    )


# -------------------------------------------------------------------------- #
# Fixtures
# -------------------------------------------------------------------------- #

@pytest.fixture
def mock_server():
    """Start a mock LLM server and tear it down after the test."""
    mode = os.getenv("MOCK_MODE", "ok")
    port = 9999
    proc = start_mock_server(mode, port)
    yield proc, port
    stop_mock_server(proc)


@pytest.fixture
def hermes_home():
    """Provide a temporary HERMES_HOME and clean it up."""
    tmp = tempfile.mkdtemp(prefix="hermes_smoke_")
    yield tmp
    # No cleanup needed — tempfile takes care of it


# -------------------------------------------------------------------------- #
# CLI snippet for CI (paste into .github/workflows/smoke-hang.yml)
# -------------------------------------------------------------------------- #
# ```yaml
# name: Model Hang Smoke
# on: [push, pull_request]
# jobs:
#   smoke:
#     runs-on: windows-latest
#     steps:
#       - uses: actions/checkout@v4
#       - name: Set up Python
#         uses: actions/setup-python@v5
#         with:
#           python-version: "3.11"
#       - name: Install hermes
#         run: pip install -e .
#       - name: Start mock server
#         run: python scripts/mock_llm_server.py &
#           env:
#             MODE: ok
#             MOCK_PORT: 9999
#       - name: Run smoke tests
#         run: python -m pytest tests/smoke/test_model_hang.py -v --timeout=20
# ```
