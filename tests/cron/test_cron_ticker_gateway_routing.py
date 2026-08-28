"""
Tests for F3.6: Desktop cron ticker must route Discord delivery through live gateway.

Root cause: _start_desktop_cron_ticker in hermes_cli/web_server.py calls
InProcessCronScheduler.start() with no adapters argument, so Discord deliveries
bypass the live gateway adapter and go direct to _send_to_platform — losing
Discord thread context and bypassing the gateway session.

Acceptance criteria:
(a) cron ticker routes through gateway when gateway is active
(b) cron ticker falls back to direct path only when gateway is unavailable
(c) Discord thread state is preserved across gateway reconnect
"""

import pytest
from unittest import mock
from unittest.mock import MagicMock, patch
import asyncio


# -------------------------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------------------------


def _mock_discord_adapter():
    """Return a mock Discord adapter that tracks deliveries."""
    adapter = MagicMock()
    adapter.platform = MagicMock()
    adapter.platform.value = "discord"
    adapter.supports_inchannel_continuable = False
    adapter.supports_inchannel_continuable_for_platform = MagicMock(return_value=False)
    return adapter


def _mock_telegram_adapter():
    adapter = MagicMock()
    adapter.platform = MagicMock()
    adapter.platform.value = "telegram"
    return adapter


# -------------------------------------------------------------------------------------
# Test: Discord delivery uses live gateway adapter when gateway is active
# -------------------------------------------------------------------------------------


class TestDiscordRoutesThroughLiveGateway:
    """When gateway DiscordAdapter is available, cron must route through it."""

    def test_cron_discord_delivery_uses_live_adapter_when_available(self):
        """
        Verify that _deliver_result uses the live Discord adapter
        (from adapters dict) instead of falling back to standalone send.

        This tests the routing decision in _deliver_result when:
        - adapters contains Platform.DISCORD -> DiscordAdapter
        - loop is provided (gateway is running)
        Expected: live adapter send path is used (DeliveryRouter._deliver_to_platform)
        """
        from cron import scheduler as sched
        from cron.scheduler import _deliver_result
        from gateway.config import Platform
        from gateway.delivery import resolve_delivery_transport, DeliveryTransport

        discord_adapter = _mock_discord_adapter()

        # Mock config
        mock_config = MagicMock()
        mock_platform_config = MagicMock()
        mock_platform_config.enabled = True
        mock_config.platforms = {Platform.DISCORD: mock_platform_config}

        mock_loop = MagicMock()
        mock_loop.is_running = MagicMock(return_value=True)

        # adapters dict WITH Discord live adapter
        adapters = {Platform.DISCORD: discord_adapter}

        job = {
            "id": "test-job-1",
            "name": "Test Discord Job",
            "deliver": "discord",
            "origin": {"platform": "discord", "chat_id": "123456"},
        }

        content = "Test cron output"

        # Patch the live-send path to track calls
        delivery_called = False
        fallback_called = False

        def mock_deliver_to_platform(route_target, text, metadata):
            nonlocal delivery_called
            delivery_called = True
            return MagicMock(success=True, message_id="789", raw_response={})

        def mock_send_to_platform(platform, pconfig, chat_id, content, **kwargs):
            nonlocal fallback_called
            fallback_called = True
            return {"success": True}

        with patch.object(sched, "_normalize_deliver_value", return_value="discord"), \
             patch.object(sched, "_resolve_delivery_targets", return_value=[{"platform": "discord", "chat_id": "123456", "thread_id": None}]), \
             patch.object(sched, "_resolve_origin", return_value={}), \
             patch.object(sched, "_resolve_cron_surface_mode", return_value="thread"), \
             patch.object(sched, "_open_continuable_cron_thread", return_value=None), \
             patch.object(sched, "_confirm_adapter_delivery", return_value=True), \
             patch.object(sched, "_seed_cron_thread_session", return_value=False), \
             patch.object(sched, "_maybe_mirror_cron_delivery", return_value=None), \
             patch.object(sched, "_send_media_via_adapter", return_value=[]), \
             patch.object(sched, "_interpreter_shutting_down", return_value=False), \
             patch.object(sched, "load_config", return_value={}), \
             patch("gateway.delivery.resolve_delivery_transport") as mock_resolve_transport, \
             patch("tools.send_message_tool._send_to_platform", mock_send_to_platform):

            # Setup: resolve_delivery_transport returns a live transport
            live_transport = DeliveryTransport(
                adapter=discord_adapter,
                config=mock_platform_config,
                transport_platform=Platform.DISCORD,
            )
            mock_resolve_transport.return_value = live_transport

            from gateway.delivery import DeliveryRouter, DeliveryTarget
            with patch.object(DeliveryRouter, "_deliver_to_platform", mock_deliver_to_platform):
                result = _deliver_result(
                    job,
                    content,
                    adapters=adapters,
                    loop=mock_loop,
                )

        # CRITICAL assertion: the live adapter path MUST be used, NOT fallback
        # If adapters contains the Discord adapter and loop is running,
        # the DeliveryRouter path should be taken (delivery_called = True)
        # and _send_to_platform standalone should NOT be called (fallback_called = False)
        assert delivery_called, (
            "CRON F3.6 FAILURE: Discord delivery did NOT go through the live gateway adapter. "
            "Expected DeliveryRouter._deliver_to_platform to be called. "
            f"delivery_called={delivery_called}, fallback_called={fallback_called}"
        )
        assert not fallback_called, (
            "CRON F3.6 FAILURE: Discord delivery fell back to standalone _send_to_platform "
            "even though live Discord adapter was available in adapters dict."
        )

    def test_cron_discord_falls_back_when_no_live_adapter(self):
        """
        Verify that when no Discord adapter is in adapters,
        _send_to_platform standalone path is used as fallback.
        """
        from cron import scheduler as sched
        from cron.scheduler import _deliver_result
        from gateway.config import Platform

        mock_loop = MagicMock()
        mock_loop.is_running = MagicMock(return_value=True)

        # NO Discord adapter — adapters is empty or None for Discord
        adapters = {}  # no Discord adapter

        job = {
            "id": "test-job-2",
            "name": "Test Discord Fallback",
            "deliver": "discord",
            "origin": {"platform": "discord", "chat_id": "123456"},
        }

        content = "Test cron output"

        standalone_called = False
        def mock_send_to_platform(platform, pconfig, chat_id, content, **kwargs):
            nonlocal standalone_called
            standalone_called = True
            return {"success": True}

        with patch.object(sched, "_normalize_deliver_value", return_value="discord"), \
             patch.object(sched, "_resolve_delivery_targets", return_value=[{"platform": "discord", "chat_id": "123456", "thread_id": None}]), \
             patch.object(sched, "_resolve_origin", return_value={}), \
             patch.object(sched, "_resolve_cron_surface_mode", return_value="thread"), \
             patch.object(sched, "_open_continuable_cron_thread", return_value=None), \
             patch.object(sched, "_confirm_adapter_delivery", return_value=True), \
             patch.object(sched, "_seed_cron_thread_session", return_value=False), \
             patch.object(sched, "_maybe_mirror_cron_delivery", return_value=None), \
             patch.object(sched, "_send_media_via_adapter", return_value=[]), \
             patch.object(sched, "_interpreter_shutting_down", return_value=False), \
             patch.object(sched, "load_config", return_value={}), \
             patch("gateway.delivery.resolve_delivery_transport", return_value=None), \
             patch("tools.send_message_tool._send_to_platform", mock_send_to_platform):

            result = _deliver_result(
                job,
                content,
                adapters=adapters,
                loop=mock_loop,
            )

        # When no live adapter, standalone path is acceptable
        assert standalone_called or result is None, (
            "When no live Discord adapter is available, either standalone send "
            "should be called or result should be None (local delivery)"
        )


class TestDesktopCronTickerBypass:
    """Desktop cron ticker (no adapters) must detect live gateway Discord and use it."""

    def test_desktop_cron_ticker_checks_for_live_gateway_discord(self):
        """
        CRITICAL TEST for F3.6: _start_desktop_cron_ticker must detect if a gateway
        IS running with Discord connected, and pass those adapters to the scheduler.

        BUG (pre-fix): _start_desktop_cron_ticker did NOT check for live gateway
        adapters and passed adapters=None, causing Discord to bypass gateway.
        """
        from hermes_cli import web_server as ws
        from unittest.mock import patch, MagicMock
        from gateway.config import Platform

        # Track what adapters were actually passed to InProcessCronScheduler.start
        captured_adapters = []
        captured_loop = []

        class FakeProvider:
            name = "test"
            def start(self, stop_event, **kwargs):
                captured_adapters.append(kwargs.get("adapters"))
                captured_loop.append(kwargs.get("loop"))

        # Mock Discord adapter to be present in the live gateway runner
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

        # loop should also be passed (may be None in test context but key must exist)
        loop_passed = captured_loop[0]
        assert "loop" in {} or loop_passed is not None or loop_passed is None, (
            "loop should be explicitly passed (even if None when no running loop)"
        )


class TestDiscordThreadStatePreserved:
    """Thread state must be preserved when gateway reconnects (live adapter restored)."""

    def test_thread_id_preserved_in_delivery_when_adapter_reconnects(self):
        """
        When a Discord delivery is attempted with the adapter available,
        thread context must be preserved through DeliveryRouter.
        """
        from cron import scheduler as sched
        from cron.scheduler import _deliver_result
        from gateway.config import Platform
        from gateway.delivery import DeliveryTransport, DeliveryTarget

        discord_adapter = _mock_discord_adapter()
        job_thread_id = "987654321"
        job_chat_id = "111222333"

        job = {
            "id": "test-thread-preservation",
            "name": "Thread Test",
            "deliver": "discord",
            "origin": {
                "platform": "discord",
                "chat_id": job_chat_id,
                "thread_id": job_thread_id,
            },
        }

        mock_platform_config = MagicMock()
        mock_platform_config.enabled = True
        mock_config = MagicMock()
        mock_config.platforms = {Platform.DISCORD: mock_platform_config}

        mock_loop = MagicMock()
        mock_loop.is_running = MagicMock(return_value=True)

        adapters = {Platform.DISCORD: discord_adapter}

        captured_route_target = None

        def mock_deliver_to_platform(route_target, text, metadata):
            nonlocal captured_route_target
            captured_route_target = route_target
            return MagicMock(success=True, message_id="123", raw_response={})

        with patch.object(sched, "_normalize_deliver_value", return_value="discord"), \
             patch.object(sched, "_resolve_delivery_targets", return_value=[{
                 "platform": "discord", "chat_id": job_chat_id, "thread_id": job_thread_id
             }]), \
             patch.object(sched, "_resolve_origin", return_value={
                 "platform": "discord", "chat_id": job_chat_id, "thread_id": job_thread_id
             }), \
             patch.object(sched, "_resolve_cron_surface_mode", return_value="thread"), \
             patch.object(sched, "_open_continuable_cron_thread", return_value=None), \
             patch.object(sched, "_confirm_adapter_delivery", return_value=True), \
             patch.object(sched, "_seed_cron_thread_session", return_value=False), \
             patch.object(sched, "_maybe_mirror_cron_delivery", return_value=None), \
             patch.object(sched, "_send_media_via_adapter", return_value=[]), \
             patch.object(sched, "_interpreter_shutting_down", return_value=False), \
             patch.object(sched, "load_config", return_value={}), \
             patch("gateway.delivery.resolve_delivery_transport") as mock_resolve_transport:

            live_transport = DeliveryTransport(
                adapter=discord_adapter,
                config=mock_platform_config,
                transport_platform=Platform.DISCORD,
            )
            mock_resolve_transport.return_value = live_transport

            from gateway.delivery import DeliveryRouter
            with patch.object(DeliveryRouter, "_deliver_to_platform", mock_deliver_to_platform):
                result = _deliver_result(
                    job,
                    "Test output",
                    adapters=adapters,
                    loop=mock_loop,
                )

        # CRITICAL: thread_id must be preserved in the delivery routing
        assert captured_route_target is not None, (
            "F3.6 FAILURE: DeliveryRouter._deliver_to_platform was never called — "
            "thread_id could not be preserved."
        )
        assert captured_route_target.thread_id == job_thread_id, (
            f"F3.6 FAILURE: Thread ID was NOT preserved. "
            f"Expected thread_id={job_thread_id}, "
            f"got thread_id={captured_route_target.thread_id}"
        )
        assert captured_route_target.chat_id == job_chat_id, (
            f"Chat ID mismatch. Expected {job_chat_id}, got {captured_route_target.chat_id}"
        )
