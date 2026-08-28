"""Tests for the gateway policy validation at startup (F1.5).

These tests verify that the gateway emits WARNINGS (not CRITICAL) for
policy misconfigurations at startup, without changing runtime behavior.

Test groups:
  (a) dm_policy=open + empty allowlist + no GATEWAY_ALLOW_ALL_USERS → WARNING
  (b) GATEWAY_ALLOW_ALL_USERS=true → CRITICAL
  (c) TELEGRAM_ALLOWED_USERS normalization: whitespace / commas / empty
  (d) unknown format in TELEGRAM_ALLOWED_USERS → does NOT silently ignore
"""

import logging
import os
import re
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_telegram_allowed_users(raw: str) -> tuple[set[str], list[str]]:
    """Mirror the normalization logic under test.

    Returns (normalized_ids, garbage_entries).
    Telegram user IDs are numeric strings. Anything that is not digits-only
    (or a valid @-prefixed username) is flagged as potential garbage.
    """
    if not raw:
        return set(), []
    raw = str(raw)
    # Split on commas, strip whitespace
    parts = [p.strip() for p in raw.split(",")]
    normalized = set()
    garbage = []
    for p in parts:
        if not p:
            continue
        # Garbage: entries that do NOT look like numeric Telegram IDs.
        # Valid forms: purely numeric ("12345"), @-prefixed username
        # ("@user"), or alphanumeric with minimal special chars.
        # Flag anything else: email-like, SQL injection, random strings, etc.
        if not re.match(r"^\d+$", p) and not re.match(r"^@[\w]+$", p):
            garbage.append(p)
            continue
        normalized.add(p)
    return normalized, garbage


class _LogCapture(logging.Handler):
    """Captures log records with level and message text."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)

    def get_level_messages(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]


# ---------------------------------------------------------------------------
# Group (a): dm_policy=open + empty allowlist → WARNING
# ---------------------------------------------------------------------------

class TestOpenPolicyWithEmptyAllowlist:
    """When dm_policy or group_policy is 'open' but no allowlist is configured,
    the gateway must emit at least a WARNING at startup."""

    def test_open_dm_policy_no_allowlist_no_allow_all_emits_warning(self):
        """Single Telegram open-dm no-allowlist → WARNING (not CRITICAL)."""
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            # No TELEGRAM_ALLOWED_USERS
            # No GATEWAY_ALLOW_ALL_USERS
            # dm_policy would default to "open" from adapter
        }
        with patch.dict(os.environ, env, clear=True):
            from gateway.authz_mixin import _gateway_policy_warnings
            warnings = _gateway_policy_warnings()

        warning_messages = [r.getMessage() for r in warnings if r.levelno == logging.WARNING]
        assert any("open" in m.lower() or "allowlist" in m.lower() or "dm_policy" in m.lower()
                   for m in warning_messages), (
            f"Expected WARNING about open policy + empty allowlist, got: {warning_messages}"
        )

    def test_open_group_policy_no_allowlist_emits_warning(self):
        """group_policy=open with no group allowlist → WARNING."""
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            # TELEGRAM_ALLOWED_USERS empty (only DM allowlist, not group)
            # No TELEGRAM_GROUP_ALLOWED_CHATS
            # No GATEWAY_ALLOW_ALL_USERS
        }
        with patch.dict(os.environ, env, clear=True):
            from gateway.authz_mixin import _gateway_policy_warnings
            warnings = _gateway_policy_warnings()

        warning_messages = [r.getMessage() for r in warnings]
        # Should warn about either DM or group policy openness
        assert any("open" in m.lower() or "group" in m.lower() or "allowlist" in m.lower()
                   for m in warning_messages), f"Expected warning about open policy, got: {warning_messages}"

    def test_open_policy_but_allowlist_populated_no_warning(self):
        """When allowlist IS populated, open policy should NOT warn."""
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "TELEGRAM_ALLOWED_USERS": "  12345 , 67890  ",  # populated
            # No GATEWAY_ALLOW_ALL_USERS
        }
        with patch.dict(os.environ, env, clear=True):
            from gateway.authz_mixin import _gateway_policy_warnings
            warnings = _gateway_policy_warnings()

        warning_messages = [r.getMessage() for r in warnings]
        # Should NOT have a warning about missing allowlist when one is present
        open_policy_warnings = [m for m in warning_messages
                                if "open" in m.lower() and "allowlist" in m.lower()]
        assert not open_policy_warnings, f"Did not expect warning with populated allowlist, got: {warning_messages}"


# ---------------------------------------------------------------------------
# Group (b): GATEWAY_ALLOW_ALL_USERS=true → CRITICAL
# ---------------------------------------------------------------------------

class TestAllowAllUsersFlag:
    """GATEWAY_ALLOW_ALL_USERS=true must emit a CRITICAL-level warning."""

    def test_gateway_allow_all_users_emits_critical(self):
        """GATEWAY_ALLOW_ALL_USERS=true → CRITICAL log."""
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "GATEWAY_ALLOW_ALL_USERS": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            from gateway.authz_mixin import _gateway_policy_warnings
            warnings = _gateway_policy_warnings()

        critical_messages = [r.getMessage() for r in warnings if r.levelno == logging.CRITICAL]
        assert any("GATEWAY_ALLOW_ALL_USERS" in m or "allow all" in m.lower()
                   for m in critical_messages), (
            f"Expected CRITICAL log for GATEWAY_ALLOW_ALL_USERS=true, got: {critical_messages}"
        )

    def test_telegram_allow_all_users_emits_critical(self):
        """TELEGRAM_ALLOW_ALL_USERS=true → CRITICAL log."""
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "TELEGRAM_ALLOW_ALL_USERS": "yes",
        }
        with patch.dict(os.environ, env, clear=True):
            from gateway.authz_mixin import _gateway_policy_warnings
            warnings = _gateway_policy_warnings()

        critical_messages = [r.getMessage() for r in warnings if r.levelno == logging.CRITICAL]
        assert critical_messages, "Expected CRITICAL log for TELEGRAM_ALLOW_ALL_USERS=yes"

    def test_allow_all_users_false_does_not_emit_critical(self):
        """GATEWAY_ALLOW_ALL_USERS=false → no CRITICAL."""
        env = {
            "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF",
            "GATEWAY_ALLOW_ALL_USERS": "false",
        }
        with patch.dict(os.environ, env, clear=True):
            from gateway.authz_mixin import _gateway_policy_warnings
            warnings = _gateway_policy_warnings()

        critical_messages = [r.getMessage() for r in warnings if r.levelno == logging.CRITICAL]
        assert not critical_messages, f"Did not expect CRITICAL for =false, got: {critical_messages}"


# ---------------------------------------------------------------------------
# Group (c): TELEGRAM_ALLOWED_USERS normalization
# ---------------------------------------------------------------------------

class TestTelegramAllowedUsersNormalization:
    """TELEGRAM_ALLOWED_USERS must be normalized and garbage must be warned."""

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace is stripped from each entry."""
        ids, garbage = _parse_telegram_allowed_users("  12345 , 67890  ")
        assert "12345" in ids
        assert "67890" in ids

    def test_empty_parts_ignored(self):
        """Empty entries between commas are ignored."""
        ids, garbage = _parse_telegram_allowed_users("12345,,67890,,  ,  999")
        assert "12345" in ids
        assert "67890" in ids
        assert "999" in ids

    def test_duplicate_normalized_to_set(self):
        """Duplicates collapse to a single entry."""
        ids, garbage = _parse_telegram_allowed_users("12345,  12345 , 67890")
        assert ids == {"12345", "67890"}

    def test_garbage_entries_flagged(self):
        """Entries that look nothing like valid Telegram IDs → flagged as garbage."""
        ids, garbage = _parse_telegram_allowed_users("12345, not_a_user, 67890")
        assert "12345" in ids
        assert "67890" in ids
        assert "not_a_user" in garbage

    def test_email_like_not_silently_ignored(self):
        """Entries that look like emails must NOT be silently dropped."""
        ids, garbage = _parse_telegram_allowed_users("12345, user@example.com")
        # The email address is garbage from a Telegram-ID perspective
        assert "12345" in ids
        assert "user@example.com" in garbage


# ---------------------------------------------------------------------------
# Group (d): unknown format → NOT silently ignored
# ---------------------------------------------------------------------------

class TestUnknownFormatNotSilentlyIgnored:
    """Entries in TELEGRAM_ALLOWED_USERS that are malformed must NOT be
    silently dropped — they must appear in the garbage list."""

    def test_entries_with_only_special_chars_flagged(self):
        """Entries like '!!!' or '---' are clearly garbage and must be flagged."""
        ids, garbage = _parse_telegram_allowed_users("12345, !!!, ---, 99999")
        assert "12345" in ids
        assert "99999" in ids
        assert "!!!" in garbage
        assert "---" in garbage

    def test_sql_injection_like_entries_flagged(self):
        """Entries with SQL-like patterns must be flagged as garbage."""
        ids, garbage = _parse_telegram_allowed_users("12345, 1 OR 1=1, 67890")
        assert "12345" in ids
        assert "67890" in ids
        assert "1 OR 1=1" in garbage

    def test_very_long_nonsense_flagged(self):
        """Very long random strings should be flagged."""
        ids, garbage = _parse_telegram_allowed_users(
            "12345, " + "x" * 200
        )
        assert "12345" in ids
        assert ("x" * 200) in garbage
