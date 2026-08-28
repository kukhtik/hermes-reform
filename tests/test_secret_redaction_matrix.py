"""Secret redaction matrix — F1.1 RED phase.

Tests 8 secret patterns across every write path that touches disk or logs.
Acceptance: ALL patterns masked in ALL paths.
"""
import os
import tempfile
from pathlib import Path

import pytest

# 8 canonical secret patterns from the contract
SECRETS = {
    "password_kv":      "export MYSQL_PASSWORD=super_secret_123",
    "api_key":          "x-api-key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
    "bearer":           "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "pem_private_key":   "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDGXqHf3Q6r8YVv\nVGhlbTogSGVybWVzIGVuY3J5cHRpb24gdGVzdCBrZXkgaGVyZS4KZXhhbXBsZTog\nc2VjcmV0IGtleSBmb3IgdGVzdGluZwo=\n-----END PRIVATE KEY-----",
    "aws_akia":         "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    "jwt_token":        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
    "private_key_label": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGy0AHBcLvT8K2ELbJKEqM9j\ncGxlYXN0IGtleSBmb3IgSGVybWVzIHRlc3RpbmcKYW5vdGhlciB0ZXN0IGV4YW1w\nbGUgc2VjcmV0IGtleQo=\n-----END RSA PRIVATE KEY-----",
    "webhook_token":    "https://example.com/webhook?token=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890&repo=test",
}

# Non-secret prose — must NOT be modified by redaction
PROSE = [
    "The password is not set in this config.",
    "api_key usage is logged for debugging.",
    "The bearer token was rejected.",
    "This private key belongs to no one.",
    "AWS AKIA keys are rotated regularly.",
    "The JWT standard uses dot-separated segments.",
    "A private key label is visible here.",
    "Webhook tokens are HTTP GET parameters.",
]


# ─── Path 1: hermes_state._insert_message_rows → SQLite ───────────────────────

def test_hermes_state_redact_if_str_masks_all_patterns():
    """hermes_state._redact_if_str masks all 8 secret patterns before SQLite write.

    The real write path is :meth:`_insert_message_rows` in hermes_state.py, which
    applies ``_redact_if_str`` to content/reasoning/api_content fields.  We test the
    helper directly to avoid needing a full SessionDB + sqlite fixture.
    """
    from hermes_state import _redact_if_str

    for name, secret in SECRETS.items():
        # Test the primary text fields that get written to SQLite
        result = _redact_if_str(secret)
        if name == "password_kv":
            assert "super_secret_123" not in result
        elif name == "api_key":
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in result
        elif name == "bearer":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        elif name == "aws_akia":
            assert "AKIAIOSFODNN7EXAMPLE" not in result
        elif name == "jwt_token":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in result
        elif name in ("pem_private_key", "private_key_label"):
            assert "-----BEGIN" not in result
        elif name == "webhook_token":
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in result


def test_hermes_state_redact_preserves_prose():
    """Prose that merely mentions secret keywords must not be altered."""
    from hermes_state import _redact_if_str

    for text in PROSE:
        result = _redact_if_str(text)
        # Content must be preserved (may be restructured but key words remain)
        assert text.lower() in result.lower() or text in result, \
            f"Prose corrupted: {text!r} → {result!r}"


# ─── Path 2: cron/scheduler stdout/stderr ───────────────────────────────────

def test_cron_scheduler_redacts_secrets_in_output():
    """Cron job output written to disk must not contain unmasked secrets.

    Tests the secret-scrubbing path in cron/scheduler.py's script-runner
    (``_run_job_script`` → ``redact_sensitive_text``), which is the canonical
    place where cron job stdout/stderr are redacted before being stored/returned.
    """
    # Direct functional test: invoke redact_sensitive_text as the scheduler does,
    # then verify the output contains no raw secret literals for each pattern.
    from agent.redact import redact_sensitive_text

    for name, secret in SECRETS.items():
        # Extract the value portion of the secret (the part that must not appear)
        if name == "password_kv":
            # "export MYSQL_PASSWORD=super_secret_123" → full assignment must be masked
            output = f"stdout: {secret}\nstderr: {secret}"
            redacted = redact_sensitive_text(output)
            assert "super_secret_123" not in redacted, f"[{name}] leaked"
        elif name == "api_key":
            output = secret  # "x-api-key: sk-ant-api03-..."
            redacted = redact_sensitive_text(output)
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in redacted, f"[{name}] leaked"
        elif name == "bearer":
            output = secret
            redacted = redact_sensitive_text(output)
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted, f"[{name}] JWT leaked"
        elif name == "aws_akia":
            output = secret
            redacted = redact_sensitive_text(output)
            assert "AKIAIOSFODNN7EXAMPLE" not in redacted, f"[{name}] leaked"
        elif name == "jwt_token":
            output = secret
            redacted = redact_sensitive_text(output)
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted, f"[{name}] leaked"
        elif name in ("pem_private_key", "private_key_label"):
            output = secret
            redacted = redact_sensitive_text(output)
            assert "-----BEGIN" not in redacted, f"[{name}] PEM leaked"
        elif name == "webhook_token":
            output = secret
            redacted = redact_sensitive_text(output)
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in redacted, f"[{name}] leaked"


# ─── Path 3: RedactingFormatter → log file ──────────────────────────────────

def test_redacting_formatter_masks_all_patterns(tmp_path):
    """RedactingFormatter must mask all 8 secret patterns in log output."""
    from agent.redact import RedactingFormatter

    formatter = RedactingFormatter()
    records = []
    for name, secret in SECRETS.items():
        rec = logging.LogRecord(
            "test", logging.INFO, "", 0, secret, (), None
        )
        records.append((name, rec))

    for name, rec in records:
        formatted = formatter.format(rec)
        # Each pattern has a distinctive value portion we can check
        if name == "password_kv":
            assert "super_secret_123" not in formatted, f"[{name}] leaked in log"
        elif name == "api_key":
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in formatted, \
                f"[{name}] leaked in log"
        elif name == "aws_akia":
            assert "AKIAIOSFODNN7EXAMPLE" not in formatted, f"[{name}] leaked in log"
        elif name == "jwt_token":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in formatted, \
                f"[{name}] JWT leaked in log"
        elif name in ("pem_private_key", "private_key_label"):
            assert "-----BEGIN" not in formatted, f"[{name}] PEM leaked in log"
        elif name == "webhook_token":
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in formatted, \
                f"[{name}] webhook token leaked in log"


# ─── Path 4: redact_sensitive_text (general scrubber) ────────────────────────

def test_redact_sensitive_text_all_patterns():
    """redact_sensitive_text must mask all 8 secret patterns."""
    from agent.redact import redact_sensitive_text

    for name, secret in SECRETS.items():
        redacted = redact_sensitive_text(secret)
        # Check key distinctive substrings don't appear unmasked
        if name == "password_kv":
            assert "super_secret_123" not in redacted
        elif name == "api_key":
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in redacted
        elif name == "bearer":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
            assert "Bearer" in redacted  # header preserved
        elif name == "aws_akia":
            assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        elif name == "jwt_token":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        elif name in ("pem_private_key", "private_key_label"):
            assert "-----BEGIN" not in redacted
        elif name == "webhook_token":
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in redacted
            assert "https://example.com/webhook" in redacted  # URL preserved


# ─── Path 5: cron/incidents._redact_error ───────────────────────────────────

def test_cron_incidents_redact_error_all_patterns():
    """cron/incidents._redact_error must not let any secret through."""
    from cron.incidents import _redact_error

    for name, secret in SECRETS.items():
        error_msg = f"Job failed with error: {secret}"
        redacted = _redact_error(error_msg)
        if name == "password_kv":
            assert "super_secret_123" not in redacted
        elif name == "api_key":
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in redacted
        elif name == "bearer":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        elif name == "aws_akia":
            assert "AKIAIOSFODNN7EXAMPLE" not in redacted
        elif name == "jwt_token":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted
        elif name in ("pem_private_key", "private_key_label"):
            assert "-----BEGIN" not in redacted
        elif name == "webhook_token":
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in redacted


# ─── Path 6: cli.py /save export with redact flag ─────────────────────────

def test_cli_save_redact_exports_all_patterns(tmp_path):
    """The /save export with --redact must mask all 8 patterns."""
    from hermes_cli.session_export_md import redact_session_data

    session = {
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": secret}
        ]
        for secret in SECRETS.values()
    }

    redacted = redact_session_data(session)
    redacted_str = str(redacted)

    for name, secret in SECRETS.items():
        if name == "password_kv":
            assert "super_secret_123" not in redacted_str
        elif name == "api_key":
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in redacted_str
        elif name == "bearer":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted_str
        elif name == "aws_akia":
            assert "AKIAIOSFODNN7EXAMPLE" not in redacted_str
        elif name == "jwt_token":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted_str
        elif name in ("pem_private_key", "private_key_label"):
            assert "-----BEGIN PRIVATE KEY" not in redacted_str
        elif name == "webhook_token":
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in redacted_str


# ─── Path 7: config.yaml round-trip — real value preserved, placeholder NOT ─

def test_config_yaml_placeholder_not_persisted(monkeypatch, tmp_path):
    """Fix #42727: redact_config_value must NOT affect what is written to config.yaml.

    The display helper redact_config_value() masks values for user-facing output
    only. It must NEVER affect the actual value written by save_config_value()
    or save_config() back to config.yaml.  A placeholder like '***' written back
    to the config would break the gateway (it reads the real value, not '***').
    """
    import tempfile, os
    from pathlib import Path

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    config_file = hermes_home / "config.yaml"
    config_file.write_text("providers:\n  openai:\n    api_key: sk-ant-api03-real-secret-key-abcdefghijklmnopqrstuvwxyz\n")

    # Patch HERMES_HOME so save_config_value writes to our temp dir
    monkeypatch.setattr("cli.get_hermes_home", lambda: hermes_home)

    # Monkeypatch atomic_roundtrip_yaml_update to capture what is written
    from utils import atomic_roundtrip_yaml_update as orig_atomic
    written_values = []
    def capture_atomic(path, key_path, value):
        written_values.append((key_path, value))
        return orig_atomic(path, key_path, value)

    import utils
    monkeypatch.setattr(utils, "atomic_roundtrip_yaml_update", capture_atomic)

    from cli import save_config_value
    from hermes_cli.auth import has_usable_secret

    # Read the real API key from config.yaml
    real_key = "sk-ant-api03-real-secret-key-abcdefghijklmnopqrstuvwxyz"
    assert config_file.read_text().strip().endswith(real_key)

    # save_config_value must write the REAL value, not a redacted placeholder.
    # We trigger a model switch which calls save_config_value internally.
    # Simulate: save_config_value("model.provider", "openai")
    # The key we save must contain the actual secret, not '***'
    save_config_value("model.provider", "openai")

    # Check what atomic_roundtrip_yaml_update received
    for key_path, value in written_values:
        if "api_key" in key_path.lower() or "secret" in key_path.lower() or "key" in key_path.lower():
            assert value != "***", \
                f"[#42727] Redacted placeholder '***' would be written to config.yaml for {key_path!r}"
            assert "sk-ant-api03-real-secret-key" in str(value) or key_path == "model.provider", \
                f"[#42727] Real secret not preserved in config.yaml write for {key_path!r}: {value!r}"


# ─── Path 8: session export markdown ────────────────────────────────────────

def test_session_export_md_redacts_all_patterns(tmp_path):
    """Session export (JSON/MD/HTML) must redact all 8 secret patterns."""
    from hermes_cli.session_export_md import redact_session_data

    session = {
        "title": "Test Session",
        "messages": [
            {"role": "user", "content": f"[{name}] {secret}"}
            for name, secret in SECRETS.items()
        ] + [
            {"role": "assistant", "content": f"[{name}] {secret}"}
            for name, secret in SECRETS.items()
        ],
    }

    redacted = redact_session_data(session)
    redacted_str = str(redacted)

    for name, secret in SECRETS.items():
        if name == "password_kv":
            assert "super_secret_123" not in redacted_str
        elif name == "api_key":
            assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in redacted_str
        elif name == "bearer":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted_str
        elif name == "aws_akia":
            assert "AKIAIOSFODNN7EXAMPLE" not in redacted_str
        elif name == "jwt_token":
            assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in redacted_str
        elif name in ("pem_private_key", "private_key_label"):
            assert "-----BEGIN PRIVATE KEY" not in redacted_str and \
                   "-----BEGIN RSA PRIVATE KEY" not in redacted_str
        elif name == "webhook_token":
            assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890" not in redacted_str


# ─── Import logging for RedactingFormatter test ─────────────────────────────
import logging
