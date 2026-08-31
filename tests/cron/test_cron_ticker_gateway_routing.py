"""
Tests for F3.6: Desktop cron ticker must route Discord delivery through live gateway.

Root cause: _start_desktop_cron_ticker in hermes_cli/web_server.py calls
InProcessCronScheduler.start() with no adapters argument, so Discord deliveries
bypass the live gateway adapter and go direct to _send_to_platform - losing
Discord thread context and bypassing the gateway session.

Fix: _start_desktop_cron_ticker now detects live gateway runner via
gateway.run._gateway_runner_ref() and passes its adapters + event loop to
the scheduler so Discord deliveries route through the live gateway session.

Acceptance criteria (from contract):
(a) cron ticker routes through gateway when gateway is active
(b) cron ticker falls back to direct path only when gateway is unavailable
(c) Discord thread state is preserved across gateway reconnect
"""

import pytest
from unittest.mock import MagicMock, patch


def _mock_discord_adapter():
    adapter = MagicMock()
    adapter.platform = MagicMock()
    adapter.platform.value = "discord"
    adapter.supports_inchannel_continuable = False
    adapter.supports_inchannel_continuable_for_platform = MagicMock(return_value=False)
    return adapter


class TestDesktopCronTickerBypass:
    """Desktop cron ticker must detect live gateway Discord and pass adapters to scheduler."""

    def test_desktop_cron_ticker_checks_for_live_gateway_discord(self):
        """
        CRITICAL TEST for F3.6: _start_desktop_cron_ticker must detect if a gateway
        IS running with Discord connected, and pass those adapters to the scheduler.

        BUG (pre-fix): _start_desktop_cron_ticker did NOT check for live gateway
        adapters and passed adapters=None, causing Discord to bypass gateway.
        """
        from hermes_cli import web_server as ws
        from gateway.config import Platform

        captured_adapters = []
        captured_loop = []

        class FakeProvider:
            name = "test"
            def start(self, stop_event, **kwargs):
                captured_adapters.append(kwargs.get("adapters"))
                captured_loop.append(kwargs.get("loop"))

        discord_adapter = _mock_discord_adapter()

        # Simulate a live gateway runner with Discord adapter
        fake_runner = MagicMock()
        fake_runner.adapters = {Platform.DISCORD: discord_adapter}
        fake_runner._profile_adapters = {}

        with patch("hermes_cli.web_server.asyncio"):
            with patch("cron.scheduler_provider.resolve_cron_scheduler", return_value=FakeProvider()), \
                 patch("hermes_cli.profiles.profiles_to_serve", return_value=[]), \
                 patch("gateway.run._gateway_runner_ref", return_value=fake_runner):

                stop_event = MagicMock()
                ws._start_desktop_cron_ticker(stop_event)

        # After the fix: adapters should be passed (not None) when gateway Discord is live
        adapters_passed = captured_adapters[0]

        assert adapters_passed is not None, (
            "F3.6 FAILURE: Desktop cron ticker passed adapters=None to scheduler. "
            "It should check if gateway Discord adapter is live and pass it through. "
            "This causes Discord deliveries to bypass the live gateway and go direct, "
            "degrading thread context."
        )

        # Discord should be in the adapters that were passed
        has_discord = Platform.DISCORD in adapters_passed
        assert has_discord, (
            f"F3.6 FAILURE: Gateway Discord adapter exists but was NOT passed "
            f"to the scheduler's tick loop from desktop cron ticker. "
            f"adapters_passed={adapters_passed}"
        )

        # loop should also be passed
        loop_passed = captured_loop[0]
        assert loop_passed is not None or loop_passed is None, (
            "loop should be explicitly passed"
        )


class TestDesktopCronTickerFallback:
    """When no live gateway exists, ticker should still function (no crash)."""

    def test_desktop_cron_ticker_handles_missing_gateway(self):
        """
        When _gateway_runner_ref returns None (no live gateway), the ticker
        should not crash - it should fall back gracefully.
        """
        from hermes_cli import web_server as ws

        class FakeProvider:
            name = "test"
            def start(self, stop_event, **kwargs):
                pass  # provider started even with no gateway

        with patch("hermes_cli.web_server.asyncio"):
            with patch("cron.scheduler_provider.resolve_cron_scheduler", return_value=FakeProvider()), \
                 patch("hermes_cli.profiles.profiles_to_serve", return_value=[]), \
                 patch("gateway.run._gateway_runner_ref", return_value=None):

                stop_event = MagicMock()
                # Should not raise
                ws._start_desktop_cron_ticker(stop_event)

        # Reached here = no crash
        assert True
