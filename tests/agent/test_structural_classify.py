"""Behavioral tests for structural message classification.

These are contract tests: they assert the relationship between a message's
origin and its class, not a snapshot of any data.
"""

import pytest

from agent.structural_classify import (
    ASSISTANT_OUTPUT,
    SYSTEM_NOTIFICATION,
    TOOL_CALL,
    TOOL_RESULT,
    UNKNOWN,
    USER_REQUEST,
    USER_STEERING,
    classify_message,
    is_structural,
)


class TestRoleIsAuthoritative:
    """A bare role decides the class when no marker overrides it."""

    def test_tool_role_is_tool_result(self):
        assert classify_message(role="tool", text="some output") == TOOL_RESULT

    def test_assistant_role_is_assistant_output(self):
        assert classify_message(role="assistant", text="Here is the result.") == ASSISTANT_OUTPUT

    def test_user_role_is_user_request(self):
        assert classify_message(role="user", text="Please run the tests.") == USER_REQUEST

    def test_non_empty_tool_calls_make_it_tool_call(self):
        assert classify_message(role="assistant", text="", tool_calls=[{"id": "1"}]) == TOOL_CALL

    def test_empty_tool_calls_do_not(self):
        # An assistant turn with no calls is prose, whatever the field holds.
        for empty in ("", "[]", None):
            assert classify_message(role="assistant", text="Prose.", tool_calls=empty) == ASSISTANT_OUTPUT


class TestMarkersOverrideRole:
    """Markers are more specific than a role and win over it."""

    def test_out_of_band_steering_wins_over_user_role(self):
        # Steering arrives as role="user"; the marker is what distinguishes it.
        msg = "[OUT-OF-BAND USER MESSAGE] stop the current task"
        assert classify_message(role="user", text=msg) == USER_STEERING

    def test_background_process_notice_wins_over_user_role(self):
        msg = "[IMPORTANT: Background process proc_abc completed normally"
        assert classify_message(role="user", text=msg) == SYSTEM_NOTIFICATION

    def test_plain_background_marker_also_wins(self):
        msg = "Background process proc_abc exited 0"
        assert classify_message(role="user", text=msg) == SYSTEM_NOTIFICATION

    def test_marker_only_source_without_role_still_classifies(self):
        # Flattened transcripts have no role field.
        msg = "[OUT-OF-BAND USER MESSAGE] correction"
        assert classify_message(text=msg) == USER_STEERING


class TestMarkerOnlyFallback:
    """With no role and no marker, JSON-looking text is tool traffic."""

    @pytest.mark.parametrize("prefix", ["{", '[{"', "```"])
    def test_json_prefixes_are_tool_result(self, prefix):
        assert classify_message(text=prefix + " body") == TOOL_RESULT

    def test_unrecognized_origin_is_unknown(self):
        assert classify_message(text="plain text, no role") == UNKNOWN

    def test_whitespace_does_not_defeat_the_marker(self):
        assert classify_message(role="user", text="   [OUT-OF-BAND x]  ") == USER_STEERING


class TestStructuralContract:
    """`is_structural` names exactly the classes a rule determines fully."""

    def test_structural_classes(self):
        for kind in (TOOL_RESULT, TOOL_CALL, SYSTEM_NOTIFICATION, USER_STEERING):
            assert is_structural(kind) is True

    def test_semantic_classes_are_not_structural(self):
        for kind in (ASSISTANT_OUTPUT, USER_REQUEST, UNKNOWN):
            assert is_structural(kind) is False

    def test_every_returned_class_is_classified(self):
        # Whatever the inputs, the function never returns an unlisted label.
        known = {
            TOOL_RESULT, TOOL_CALL, SYSTEM_NOTIFICATION, USER_STEERING,
            ASSISTANT_OUTPUT, USER_REQUEST, UNKNOWN,
        }
        for role in ("tool", "assistant", "user", None, "other"):
            for text in ("", "hi", "{", "[OUT-OF-BAND x]", "Background process proc_1 ok"):
                assert classify_message(role=role, text=text) in known
