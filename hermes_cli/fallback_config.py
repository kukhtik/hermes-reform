"""Helpers for reading the effective fallback provider chain from config."""

from __future__ import annotations

import json
import sys
from typing import Any


def validate_fallback_providers(value: Any) -> list[dict[str, Any]]:
    """Validate and normalize a fallback_providers config value.

    Called at ``hermes config set fallback_providers`` time, BEFORE the value is
    written to config.yaml. Raises SystemExit with an error message on failure
    so the config file is never modified.

    Accepts:
    - A list of dicts: returned as-is.
    - A single dict: wrapped in a list and returned.
    - A string: first tried as JSON, then as YAML. Parsed and validated.
    - An empty list: allowed but the caller should emit a WARN.

    Raises:
        SystemExit: if the value cannot be parsed or has the wrong shape.

    Returns:
        A list of dict entries (never empty-string keys, never missing
        provider/model).
    """
    # None / empty sentinel
    if value is None:
        print(
            "ERROR: fallback_providers cannot be null/empty. "
            "Use an empty list [] to explicitly disable fallback, or a list of "
            "provider entries.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Normalize input: try JSON first, then YAML, then accept as-is
    normalized: Any = value
    if isinstance(value, str):
        # Try JSON
        try:
            normalized = json.loads(value)
        except json.JSONDecodeError:
            # Try YAML
            try:
                import yaml

                normalized = yaml.safe_load(value)
            except Exception:
                pass
        # If still a string, it's unparseable
        if isinstance(normalized, str):
            print(
                f"ERROR: fallback_providers must be a JSON/YAML list of provider objects; "
                f"got a string that does not parse: {value!r}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Wrap a bare dict
    if isinstance(normalized, dict):
        normalized = [normalized]

    # Now must be a list
    if not isinstance(normalized, list):
        print(
            f"ERROR: fallback_providers must be a list of provider objects; "
            f"got {type(normalized).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate each entry
    result: list[dict[str, Any]] = []
    for i, entry in enumerate(normalized):
        if not isinstance(entry, dict):
            print(
                f"ERROR: fallback_providers entry {i} must be a dict with at least "
                f"'provider' and 'model' fields; got {type(entry).__name__}",
                file=sys.stderr,
            )
            sys.exit(1)
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider:
            print(
                f"ERROR: fallback_providers entry {i} is missing 'provider'",
                file=sys.stderr,
            )
            sys.exit(1)
        if not model:
            print(
                f"ERROR: fallback_providers entry {i} is missing 'model'",
                file=sys.stderr,
            )
            sys.exit(1)
        result.append(dict(entry))

    return result


def _normalized_base_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().rstrip("/")


def resolve_entry_api_key(entry: dict[str, Any] | None) -> str | None:
    """API key for one fallback entry: inline ``api_key``, else ``key_env``.

    Mirrors the custom-provider convention (``key_env`` names the env var
    holding the key; ``api_key_env`` accepted as an alias). Returns None when
    neither yields a non-empty value, letting ``resolve_runtime_provider``
    fall through to the provider's standard credential resolution.

    ``key_env`` is resolved through ``agent.secret_scope.get_secret`` rather
    than a raw ``os.getenv`` — in a multiplexed gateway a bare env read would
    ignore the active profile's scope and can return another profile's
    credential. ``get_secret`` already implements the right fallback: it
    reads ``os.environ`` when there's no active multiplexed scope (matching
    prior single-profile behavior), and fails closed only when multiplexing
    is active with no scope installed.
    """
    if not isinstance(entry, dict):
        return None
    inline = str(entry.get("api_key") or "").strip()
    if inline:
        return inline
    key_env = str(entry.get("key_env") or entry.get("api_key_env") or "").strip()
    if key_env:
        from agent.secret_scope import get_secret

        return (get_secret(key_env) or "").strip() or None
    return None


def _iter_fallback_entries(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        return []

    entries: list[dict[str, Any]] = []
    for entry in candidates:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if not provider or not model:
            continue

        normalized = dict(entry)
        normalized["provider"] = provider
        normalized["model"] = model

        base_url = _normalized_base_url(entry.get("base_url"))
        if base_url:
            normalized["base_url"] = base_url

        entries.append(normalized)
    return entries


def _entry_identity(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("provider") or "").strip().lower(),
        str(entry.get("model") or "").strip().lower(),
        _normalized_base_url(entry.get("base_url")).lower(),
    )


def get_fallback_chain(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the effective fallback chain merged across old and new config keys.

    ``fallback_providers`` remains the primary source of truth and keeps its
    order. Legacy ``fallback_model`` entries are appended afterwards unless
    they target the same provider/model/base_url route as an earlier entry.
    The returned list always contains fresh dict copies.
    """

    config = config or {}
    chain: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for key in ("fallback_providers", "fallback_model"):
        for entry in _iter_fallback_entries(config.get(key)):
            identity = _entry_identity(entry)
            if identity in seen:
                continue
            seen.add(identity)
            chain.append(entry)

    return chain
