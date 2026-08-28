"""
Egress domain allowlist guard for terminal network commands.

Enforcement point: tools/terminal_tool.terminal_tool() calls check_egress()
BEFORE executing a command, blocking or warning on non-whitelisted domains.

Policies (security.egress_policy in config.yaml):
  allow  — log warning for unknown domains, always permit  (default)
  warn   — log warning for unknown domains, always permit
  block  — reject unknown domains with a non-zero exit + message

Scope: terminal network commands (curl, wget, nc/netcat) and Python
       requests patterns. Provider API clients are OUT OF SCOPE.

Config keys (config.yaml):
  security.egress_allow_domains  — list of allowed domain patterns
  security.egress_policy         — allow | warn | block
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known providers always allowed (hardcoded fallback when config is absent)
# ---------------------------------------------------------------------------
_KNOWN_PROVIDER_DOMAINS: set[str] = {
    "api.ollama.com",
    "api.github.com",
    "pypi.org",
    "registry.npmjs.org",
    "api.telegram.org",
    "hermes-agent.nousresearch.com",
}

# Commands that trigger URL extraction
_NETWORK_COMMANDS: set[str] = {"curl", "wget", "nc", "netcat"}

# Python patterns that indicate outbound HTTP
_PYTHON_NET_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brequests\.(?:get|post|put|patch|delete|head|options)\s*\("),
    re.compile(r"\bhttpx\.(?:get|post|put|patch|delete|head|options)\s*\("),
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\bfetch\s*\("),
    re.compile(r"\bhttp\.client\.\w+\s*\("),
    re.compile(r'\brequests\.Session\(\)\.post\s*\('),
    re.compile(r'\brequests\.Session\(\)\.get\s*\('),
]

# Optional config cache for testing
_config_cache: Optional[dict] = None


# ---------------------------------------------------------------------------
# Config access
# ---------------------------------------------------------------------------

def _read_egress_config() -> dict:
    """Return the cached security.egress section from config."""
    if _config_cache is not None:
        return _config_cache
    try:
        from hermes_cli.config import read_raw_config

        raw = read_raw_config()
        return raw.get("security", {})
    except Exception as exc:
        logger.debug("Could not read config for egress guard: %s", exc)
        return {}


def _get_allowed_domains() -> set[str]:
    cfg = _read_egress_config()
    explicit = cfg.get("egress_allow_domains", [])
    if isinstance(explicit, list):
        return set(explicit)
    return set()


def _get_policy() -> str:
    return _read_egress_config().get("egress_policy", "allow")


# ---------------------------------------------------------------------------
# Domain matching
# ---------------------------------------------------------------------------

def _normalize_domain(host: str) -> str:
    """Strip leading protocol/www and trailing path/port."""
    host = re.sub(r"^https?://", "", host)
    host = re.sub(r"^www\.", "", host)
    host = host.split("/")[0]
    host = host.split(":")[0]
    return host.lower()


def _domain_matches_pattern(domain: str, pattern: str) -> bool:
    """True if domain matches an allowlist pattern (exact or *.example.com)."""
    pattern = pattern.lower().strip()
    domain = domain.lower()
    if not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[1:]  # ".example.com"
        return domain.endswith(suffix) or domain == suffix[1:]
    return domain == pattern


def _is_domain_allowed(domain: str, allowed: set[str]) -> bool:
    if not allowed:
        # Fall back to known providers
        allowed = _KNOWN_PROVIDER_DOMAINS
    domain = _normalize_domain(domain)
    # Explicit whitelist
    for p in allowed:
        if _domain_matches_pattern(domain, p):
            return True
    # Known providers always pass
    if domain in _KNOWN_PROVIDER_DOMAINS:
        return True
    return False


# ---------------------------------------------------------------------------
# URL extraction from commands
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def _extract_urls_from_curl_wget(command: str) -> list[str]:
    """Pull URLs out of curl/wget commands."""
    # Normalize common flags that precede the URL
    # wget: wget [flags] URL
    # curl: curl [flags] URL, curl [flags] -X POST -d '...' URL, etc.
    # Strip common non-URL positional args
    cleaned = re.sub(r"-[A-Za-z]+\s+[^\s'\"<>]+(?=\s+https?://)", " ", command)
    return _URL_RE.findall(cleaned)


def _extract_host_from_python(command: str) -> list[str]:
    """Extract hosts from Python network calls."""
    hosts: list[str] = []
    # requests.get("https://host.com/...") / httpx.get(...)
    host_re = re.compile(
        r"(?:requests|httpx|urlopen|http\.client)\s*\.\s*"
        r"(?:get|post|put|patch|delete|head|options|request)\s*\("
        r"\s*[\"'](https?://([^/\s:\"']+))[\"']",
        re.IGNORECASE,
    )
    for m in host_re.finditer(command):
        hosts.append(m.group(2))
    return hosts


# ---------------------------------------------------------------------------
# Network command detection
# ---------------------------------------------------------------------------

def _extract_hosts_from_nc(command: str) -> list[str]:
    """Extract host from nc/netcat commands (nc host port ...)."""
    hosts: list[str] = []
    # nc [- options] hostname port [-s source_address]
    # netcat is the same
    tokens = command.split()
    if not tokens:
        return hosts
    # skip flags (tokens starting with -)
    args = [t for t in tokens[1:] if not t.startswith("-")]
    # first non-flag token is the host
    if args:
        host = args[0]
        # strip common nc suffixes like -p
        if host not in ("localhost", "0", "443", "80"):
            hosts.append(host)
    return hosts


def _extract_network_urls(command: str) -> list[str]:
    """Return all URLs that represent network egress in this command."""
    cmd_lower = command.lower().strip()
    # Shell out to curl / wget — extract URLs
    first = cmd_lower.split()[0] if cmd_lower else ""
    if first in ("curl", "wget"):
        return _extract_urls_from_curl_wget(command)
    if first in ("nc", "netcat"):
        return _extract_hosts_from_nc(command)
    # Python network patterns
    for pat in _PYTHON_NET_PATTERNS:
        if pat.search(command):
            return _extract_host_from_python(command)
    return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_egress(command: str) -> tuple[bool, str]:
    """
    Evaluate whether *command* is allowed to make outbound network requests.

    Returns:
        (True,  "")                             — permitted, no issue
        (True,  "warning: ...")                 — permitted, warning logged
        (False, "egress blocked: ...")          — blocked (block mode)

    This is the single enforcement gate. Call it in terminal_tool()
    before spawning any subprocess.
    """
    if not command or not command.strip():
        return True, ""

    # Fast path: no URL anywhere → not a network command
    urls = _extract_network_urls(command)
    if not urls:
        return True, ""

    policy = _get_policy()
    allowed = _get_allowed_domains()
    blocked_hosts: list[str] = []
    warned_hosts: list[str] = []

    for raw_url in urls:
        parsed = urllib.parse.urlparse(raw_url)
        host = parsed.netloc or _normalize_domain(raw_url)
        if not host:
            continue
        if _is_domain_allowed(host, allowed):
            continue
        if policy == "block":
            blocked_hosts.append(host)
        else:
            warned_hosts.append(host)

    if blocked_hosts:
        msg = (
            f"egress blocked: outbound requests to non-whitelisted domains: "
            f"{', '.join(sorted(blocked_hosts))}. "
            f"Allowed domains: {sorted(allowed) if allowed else 'known-providers'}. "
            f"Command: {command[:100]!r}"
        )
        logger.warning("[egress] %s", msg)
        return False, msg

    if warned_hosts:
        msg = (
            f"egress warning: outbound requests to non-whitelisted domains: "
            f"{', '.join(sorted(warned_hosts))}. "
            f"Allowed domains: {sorted(allowed) if allowed else 'known-providers'}."
        )
        logger.warning("[egress] %s", msg)
        return True, msg

    return True, ""
