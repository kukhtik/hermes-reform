"""Efficiency metrics for the analytics dashboard.

Answers three questions the token tables cannot:

1. **How much of the tool schema are we paying for?** Every provider request
   carries the full tool list. This module estimates that weight and what
   narrowing it (`plugins/toolset-narrow`) removes.
2. **How much of the message stream is machine traffic?** Tool results and
   tool calls are classified by rule, not by a model — this reports the share.
3. **What would a local decision judge cost?** A ~400M local model answers a
   typed question in ~2 s warm (measured) at ~1.9 GB RSS, for zero tokens.

All three figures come from measurements recorded in
`docs/middleware/structural-classification-by-rule.md` and
`docs/middleware/toolset-narrowing-app-wide.md`, applied to the profile's own
recorded usage. They are estimates, labelled as such in the payload.

Pure functions only — no DB access — so the arithmetic is unit-testable and
the router just feeds them numbers.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

# --- Measured constants (2026-09-30, Windows, CPU-only) ----------------------
#
# Serialized OpenAI tool definitions for the full core tool set. 53 core tools
# produce ~13 250 tokens; a narrowed set of ~4 lands near 1 000.
TOKENS_PER_TOOL = 250
FULL_TOOLSET_TOOLS = 53
NARROWED_TOOLSET_TOOLS = 4

# Local judge, measured on CPU (laya 421M):
#   cold call 24.0 s (loads one checkpoint)  ->  warm call 2.0 s
JUDGE_WARM_SECONDS = 2.0
JUDGE_COLD_SECONDS = 24.0
JUDGE_RSS_MB = 1919.0

# Share of stream that a rule classifies exactly (measured 88.2% structural
# over 12 000 messages; 100% coverage of the remainder by role).
_RULE_COVERAGE_MEASURED = 88.2


def estimate_tool_schema_tokens(tool_count: int = FULL_TOOLSET_TOOLS) -> int:
    """Serialized token weight of a tool schema of ``tool_count`` tools."""
    return max(0, tool_count) * TOKENS_PER_TOOL


def narrowing_savings(
    api_calls: int,
    input_tokens: int,
    *,
    full_tools: int = FULL_TOOLSET_TOOLS,
    narrowed_tools: int = NARROWED_TOOLSET_TOOLS,
) -> Dict[str, Any]:
    """Tokens (and share of input) saved by narrowing the tool schema.

    ``api_calls`` is the number of provider calls in the window; each one
    carries the schema once.
    """
    full = estimate_tool_schema_tokens(full_tools)
    narrowed = estimate_tool_schema_tokens(narrowed_tools)
    per_call = max(0, full - narrowed)
    saved = per_call * max(0, api_calls)
    share = (saved / input_tokens * 100.0) if input_tokens else 0.0
    return {
        "tokens_per_call_full": full,
        "tokens_per_call_narrowed": narrowed,
        "tokens_saved_per_call": per_call,
        "tokens_saved_total": saved,
        "share_of_input_pct": round(share, 1),
    }


def classify_counts(kinds: Iterable[str]) -> Dict[str, int]:
    """Count message kinds; unknown labels are passed through unchanged."""
    out: Dict[str, int] = {}
    for k in kinds:
        out[k] = out.get(k, 0) + 1
    return out


def structural_share(kinds: Iterable[str]) -> Dict[str, Any]:
    """Share of messages a deterministic rule classifies without a model."""
    from agent.structural_classify import is_structural

    counts = classify_counts(kinds)
    total = sum(counts.values())
    structural = sum(n for k, n in counts.items() if is_structural(k))
    return {
        "total": total,
        "structural": structural,
        "structural_pct": round(structural / total * 100.0, 1) if total else 0.0,
        "by_kind": counts,
    }


def judge_cost(judge_calls: int) -> Dict[str, Any]:
    """What running a local decision judge costs, versus paying tokens.

    ``judge_calls`` is the number of decisions that would be routed to the
    local model instead of the provider.
    """
    calls = max(0, judge_calls)
    return {
        "calls": calls,
        "tokens": 0,
        "warm_seconds_each": JUDGE_WARM_SECONDS,
        "cold_seconds_once": JUDGE_COLD_SECONDS,
        "wall_seconds_warm": round(calls * JUDGE_WARM_SECONDS, 1),
        "resident_rss_mb": JUDGE_RSS_MB,
    }


def build_efficiency(
    *,
    api_calls: int,
    input_tokens: int,
    message_kinds: Optional[Iterable[str]] = None,
    judge_calls: int = 0,
) -> Dict[str, Any]:
    """Assemble the efficiency block for the analytics payload.

    ``message_kinds`` may be omitted when the caller has no message sample;
    the structural section is then reported as unavailable rather than faked.
    """
    block: Dict[str, Any] = {
        "narrowing": narrowing_savings(api_calls, input_tokens),
        "judge": judge_cost(judge_calls),
        "measured_rule_coverage_pct": _RULE_COVERAGE_MEASURED,
        "estimated": True,
        "source": "docs/middleware/toolset-narrowing-app-wide.md, "
                  "docs/middleware/structural-classification-by-rule.md",
    }
    if message_kinds is not None:
        block["structural"] = structural_share(message_kinds)
    else:
        block["structural"] = None
    return block
