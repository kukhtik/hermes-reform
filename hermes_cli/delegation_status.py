"""`hermes delegation status` CLI subcommand.

Shows per-session child-token and API-call consumption for the current
Hermes process:

    hermes delegation status [--session-id SESSION_ID]

Source: in-memory counters (reset on new session / process restart).
Limits are read from the delegation config (hermes_cli/config_defaults.py).
"""

from __future__ import annotations

import argparse
import os
import sys


def cmd_delegation_status(args: argparse.Namespace) -> int:
    # Lazily import to avoid pulling delegate_tool into the CLI bootstrap path
    # for unrelated commands.
    try:
        from tools.delegate_tool import (
            _get_session_counters,
            _load_config,
        )
    except Exception as exc:
        print(f"Error: could not load delegation budget module: {exc}", file=sys.stderr)
        return 1

    # Resolve session ID: explicit flag > HERMES_SESSION_ID env > "current"
    session_id = (
        args.session_id
        or os.environ.get("HERMES_SESSION_ID")
        or getattr(sys, "ymsg_session_id", None)
        or "default"
    )

    cfg = _load_config()
    max_tokens = cfg.get("max_child_tokens_total", 5_000_000)
    max_api_calls = cfg.get("max_child_api_calls_total", 500)

    counters = _get_session_counters(session_id)
    consumed_tokens = counters.get("tokens", 0)
    consumed_api = counters.get("api_calls", 0)

    print(f"Delegation budget — session: {session_id}")
    print(f"  Tokens:    {consumed_tokens:>12,} / {max_tokens:,}   (source: in-memory)")
    print(f"  API calls: {consumed_api:>12,} / {max_api_calls:,}   (source: in-memory)")

    pct_tokens = consumed_tokens / max_tokens * 100 if max_tokens else 0
    pct_api = consumed_api / max_api_calls * 100 if max_api_calls else 0
    print(f"  Utilization: tokens {pct_tokens:.1f}%  |  api_calls {pct_api:.1f}%")

    return 0


def register_cli(subparsers) -> None:
    """Wire the `hermes delegation` subcommand onto the top-level subparsers."""
    p = subparsers.add_parser(
        "delegation",
        help="Delegation budget status and controls",
        description="Show per-session child-token and API-call consumption "
        "against configured limits.",
    )
    p.add_argument(
        "--session-id",
        metavar="ID",
        help="Session ID to query (default: HERMES_SESSION_ID env or 'default')",
    )
    p.set_defaults(func=cmd_delegation_status)
