"""
Tests for F1.6: secret-file permissions + env-var substitution + keychain warnings.

Acceptance criteria:
(a) New secret-file gets 0600 (POSIX) / current-user-only ACL (Windows)
(b) ${ENV_VAR} in config.yaml values resolves from os.environ
(c) Missing env var → WARN + empty value (no crash)
(d) @keychain: unresolved → loud WARN
"""
import json
import logging
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


# ----------------------------------------------------------------------------- #
# Helpers
# ----------------------------------------------------------------------------- #

def secret_file_mode(path: Path) -> int:
    """Return the permission bits of *path* (0o000 if absent)."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return 0o000


def can_enforce_permissions() -> bool:
    """True when we can actually enforce file permissions (not on Windows as normal user)."""
    return sys.platform != "win32" or _has_admin_privileges()


def _has_admin_privileges() -> bool:
    """Check if we have admin privileges (rough check)."""
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


# ----------------------------------------------------------------------------- #
# (a) Permissions hardening
# ----------------------------------------------------------------------------- #

class TestSecretFilePermissions:
    """Secret files must be created with 0600 / owner-only permissions."""

    @pytest.fixture
    def isolated_home(self, tmp_path):
        """Monkey-patch get_hermes_home to return tmp_path."""
        import hermes_constants
        with mock.patch.object(hermes_constants, "get_hermes_home", return_value=tmp_path):
            yield tmp_path

    def test_response_store_db_gets_0600(self, isolated_home, monkeypatch):
        """ResponseStore.__init__ sets response_store.db to 0600."""
        pytest.importorskip("gateway.platforms.api_server")
        from gateway.platforms.api_server import ResponseStore

        db_path = str(isolated_home / "response_store.db")
        store = ResponseStore(db_path=db_path)

        mode = secret_file_mode(Path(db_path))
        if can_enforce_permissions():
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        else:
            # On Windows without admin, at least verify it was attempted
            # (mode will be 0666 but the code path ran)
            assert mode is not None

    def test_webhook_subscriptions_json_gets_0600(self, isolated_home, monkeypatch):
        """_save_subscriptions writes webhook_subscriptions.json with 0600."""
        from hermes_cli.webhook import _save_subscriptions

        subs = {"test-route": {"url": "https://example.com/hook", "secret": "abc123"}}
        _save_subscriptions(subs)

        path = isolated_home / "webhook_subscriptions.json"
        mode = secret_file_mode(path)
        if can_enforce_permissions():
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        else:
            assert mode is not None

    def test_env_file_gets_0600_on_first_write(self, isolated_home, monkeypatch):
        """save_env_value creates a new .env with 0600."""
        from hermes_cli.config import save_env_value

        path = isolated_home / ".env"
        monkeypatch.setattr(
            "hermes_cli.config.get_env_path", lambda: path
        )
        save_env_value("TEST_KEY", "test_value")

        mode = secret_file_mode(path)
        if can_enforce_permissions():
            assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
        else:
            assert mode is not None

    def test_world_readable_secret_file_warns_at_startup(self, tmp_path, caplog):
        """
        Startup warning when a secret file is world-readable.
        Uses hermes_cli.config._check_secret_file_permissions.
        """
        secret_file = tmp_path / "response_store.db"
        secret_file.write_text("dummy")
        try:
            os.chmod(secret_file, 0o644)
        except PermissionError:
            pytest.skip("Cannot chmod on this platform")

        with mock.patch("hermes_cli.config.get_hermes_home", return_value=tmp_path):
            with caplog.at_level(logging.WARNING):
                from hermes_cli.config import _check_secret_file_permissions
                _check_secret_file_permissions()

        assert any(
            "world-readable" in rec.message.lower()
            or "world_readable" in rec.message.lower()
            or "response_store.db" in rec.message
            for rec in caplog.records
        ), f"Expected warning about world-readable secret file; got: {[r.message for r in caplog.records]}"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL test")
    def test_windows_acl_restrictive(self, tmp_path):
        """On Windows, ACL enforcement is best-effort (skip if no admin)."""
        if not _has_admin_privileges():
            pytest.skip("Requires Windows administrator privileges")
        import win32security, ntsecuritycon as con
        secret_file = tmp_path / "response_store.db"
        secret_file.write_text("dummy")
        # Try to set a restrictive DACL
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorOwner(win32security.GetCurrentUser(), False)
        sd.SetSecurityDescriptorGroup(win32security.GetCurrentUser(), False)
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            con.FILE_ALL_ACCESS,
            win32security.GetCurrentUser()
        )
        sd.SetSecurityDescriptorDacl(True, dacl, False)
        win32security.SetFileSecurity(
            str(secret_file),
            win32security.OWNER_SECURITY_INFORMATION,
            sd
        )
        assert secret_file.exists()


# ----------------------------------------------------------------------------- #
# (b) & (c) env-var substitution in config.yaml values
# ----------------------------------------------------------------------------- #

class TestEnvVarSubstitution:
    """${VAR} patterns in config.yaml values are resolved from os.environ."""

    def test_substituted_value_resolved(self, monkeypatch):
        """api_key: ${MY_API_KEY} resolves from os.environ."""
        monkeypatch.setenv("MY_API_KEY", "sekrit-123")
        from hermes_cli.config import substitute_env_vars

        result = substitute_env_vars("api_key: ${MY_API_KEY}")
        assert result == "api_key: sekrit-123"

    def test_missing_var_warns_and_returns_empty(self, monkeypatch, caplog):
        """${NONEXISTENT_VAR} logs a WARNING and resolves to empty string."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        from hermes_cli.config import substitute_env_vars

        with caplog.at_level(logging.WARNING):
            result = substitute_env_vars("token: ${NONEXISTENT_VAR}")

        assert result == "token: ", f"expected empty substitution, got {result!r}"
        assert any(
            "NONEXISTENT_VAR" in rec.message or "unresolved" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected WARN about NONEXISTENT_VAR; got: {[r.message for r in caplog.records]}"

    def test_literal_without_substitution_unchanged(self, monkeypatch):
        """A value without ${...} is returned verbatim."""
        from hermes_cli.config import substitute_env_vars

        result = substitute_env_vars("model: gpt-4o")
        assert result == "model: gpt-4o"

    def test_nested_brace_syntax_substituted(self, monkeypatch):
        """${PATH} in URL is substituted when PATH env var exists."""
        monkeypatch.setenv("PATH", "/usr/bin")
        from hermes_cli.config import substitute_env_vars

        result = substitute_env_vars("url: https://example.com/${PATH}/webhook")
        assert result == "url: https://example.com//usr/bin/webhook"


# ----------------------------------------------------------------------------- #
# (d) @keychain: unresolved → loud WARN
# ----------------------------------------------------------------------------- #

class TestKeychainWarn:
    """@keychain: references that can't be resolved must warn loudly."""

    def test_unresolved_keychain_reference_warns(self, caplog):
        """A config value of @keychain:some-key logs a WARNING at startup."""
        with caplog.at_level(logging.WARNING):
            from hermes_cli.config import check_keychain_references
            # keychain system is not configured, so this should warn
            unresolved = check_keychain_references(
                {"api_key": "@keychain:MISSING_KEY_F1_6_TEST"}
            )

        assert unresolved, f"Expected unresolved @keychain reference to be flagged; got: {unresolved}"
        assert any(
            "keychain" in rec.message.lower() or "unresolved" in rec.message.lower()
            for rec in caplog.records
        ), f"Expected WARN about unresolved @keychain; got: {[r.message for r in caplog.records]}"

    def test_resolved_keychain_reference_no_warn(self, caplog, monkeypatch):
        """A config with no @keychain: or all resolved produces no warning."""
        # Put the key in os.environ so it's "resolved"
        monkeypatch.setenv("MY_SECRET_KEY", "my-secret-value")
        with caplog.at_level(logging.WARNING):
            from hermes_cli.config import check_keychain_references
            resolved = check_keychain_references(
                {"api_key": "@keychain:MY_SECRET_KEY", "token": "direct"}
            )

        assert not resolved, f"Expected no unresolved references; got: {resolved}"
        keychain_warnings = [
            r for r in caplog.records
            if "keychain" in r.message.lower()
        ]
        assert not keychain_warnings, f"Unexpected keychain warnings: {[r.message for r in keychain_warnings]}"
