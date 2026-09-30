"""Contract tests for the analytics efficiency metrics."""

import pytest

from agent.efficiency_metrics import (
    FULL_TOOLSET_TOOLS,
    JUDGE_RSS_MB,
    NARROWED_TOOLSET_TOOLS,
    TOKENS_PER_TOOL,
    build_efficiency,
    classify_counts,
    estimate_tool_schema_tokens,
    judge_cost,
    narrowing_savings,
    structural_share,
)


class TestSchemaEstimate:
    def test_scales_with_tool_count(self):
        assert estimate_tool_schema_tokens(10) == 10 * TOKENS_PER_TOOL

    def test_zero_and_negative_are_clamped(self):
        assert estimate_tool_schema_tokens(0) == 0
        assert estimate_tool_schema_tokens(-5) == 0

    def test_full_is_larger_than_narrowed(self):
        assert (estimate_tool_schema_tokens(FULL_TOOLSET_TOOLS)
                > estimate_tool_schema_tokens(NARROWED_TOOLSET_TOOLS))


class TestNarrowingSavings:
    def test_saving_is_per_call_times_calls(self):
        r = narrowing_savings(api_calls=100, input_tokens=1_000_000)
        per = r["tokens_saved_per_call"]
        assert r["tokens_saved_total"] == per * 100

    def test_no_calls_means_no_saving(self):
        r = narrowing_savings(api_calls=0, input_tokens=1000)
        assert r["tokens_saved_total"] == 0
        assert r["share_of_input_pct"] == 0.0

    def test_share_is_zero_when_input_is_zero(self):
        # zero input -> share is 0, never a divide-by-zero
        r = narrowing_savings(api_calls=10, input_tokens=0)
        assert r["share_of_input_pct"] == 0.0

    def test_share_never_exceeds_reported_for_typical_input(self):
        # With a realistic 37k-token average call the saving is a minority share.
        calls = 100
        r = narrowing_savings(api_calls=calls, input_tokens=calls * 37_000)
        assert 0 < r["share_of_input_pct"] < 100

    def test_negative_calls_clamped(self):
        assert narrowing_savings(api_calls=-3, input_tokens=100)["tokens_saved_total"] == 0


class TestRuleCoverage:
    def test_counts_kinds(self):
        assert classify_counts(["a", "a", "b"]) == {"a": 2, "b": 1}

    def test_structural_share_uses_the_classifier(self):
        kinds = ["tool_result", "tool_call", "user_request", "assistant_output"]
        r = structural_share(kinds)
        assert r["total"] == 4
        # tool_result and tool_call are structural; the two roles are not.
        assert r["structural"] == 2
        assert r["structural_pct"] == 50.0

    def test_empty_input_is_zero_not_error(self):
        r = structural_share([])
        assert r["total"] == 0
        assert r["structural_pct"] == 0.0

    def test_unknown_kinds_are_not_structural(self):
        assert structural_share(["nonsense"])["structural"] == 0


class TestJudgeCost:
    def test_zero_calls_costs_only_residency(self):
        r = judge_cost(0)
        assert r["wall_seconds_warm"] == 0
        assert r["tokens"] == 0
        assert r["resident_rss_mb"] == JUDGE_RSS_MB

    def test_wall_time_is_linear_in_calls(self):
        one = judge_cost(1)["wall_seconds_warm"]
        ten = judge_cost(10)["wall_seconds_warm"]
        assert ten == pytest.approx(one * 10)

    def test_negative_calls_clamped(self):
        assert judge_cost(-1)["calls"] == 0


class TestBuildEfficiency:
    def test_reports_structural_when_kinds_given(self):
        b = build_efficiency(api_calls=10, input_tokens=100_000,
                             message_kinds=["tool_result", "user_request"])
        assert b["structural"]["total"] == 2

    def test_reports_null_structural_without_kinds(self):
        # No sample -> unavailable, never a fabricated zero.
        b = build_efficiency(api_calls=10, input_tokens=100_000)
        assert b["structural"] is None

    def test_is_labelled_as_estimate_with_source(self):
        b = build_efficiency(api_calls=1, input_tokens=1)
        assert b["estimated"] is True
        assert "docs/middleware/" in b["source"]

    def test_contract_keys_present(self):
        b = build_efficiency(api_calls=5, input_tokens=5000)
        assert {"narrowing", "judge", "structural", "estimated", "source"} <= set(b)
