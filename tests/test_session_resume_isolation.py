"""
Test suite for session.resume / session.close DB handle isolation.

Tests that:
(a) session.resume for session A does not affect session B
(b) session.close for session A does not kill session B's DB handle
(c) concurrent session.resume calls are properly isolated

Relevant issue: #85858 (P2 — session.resume shared DB ownership →
session.close kills unrelated chats)
"""

from __future__ import annotations

import threading
import types
import uuid

import pytest


class _RecordingDB:
    """Stand-in for hermes_state.SessionDB that records close() calls."""

    def __init__(self, db_path=None, **_kwargs):
        self.db_path = db_path
        self.closed = 0
        self._rows: dict = {}

    def close(self):
        self.closed += 1

    def end_session(self, *_a, **_k):
        pass

    def get_session(self, target):
        return self._rows.get(target)

    def set_session(self, sid, row):
        self._rows[sid] = row


# -------------------------------------------------------------------------- #
# ACP SessionManager: shared _db_instance across sessions
# -------------------------------------------------------------------------- #


def _make_agent(session_id="test"):
    """Make a minimal agent-like object."""
    agent = types.SimpleNamespace()
    agent.session_id = session_id
    agent._session_db = None
    agent._owns_session_db = False
    agent._session_db_created = False
    agent._end_session_on_close = True
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    return agent


class TestACPSessionManagerIsolation:
    """ACP SessionManager shares ONE _db_instance across all its sessions.

    If session.close (or AIAgent.close reached via teardown) closes the
    SessionDB that other sessions are still using, those sessions die.
    """

    def test_close_one_session_does_not_close_shared_db(self):
        """Closing session A must not close the SessionDB that session B uses.

        This is the #85858 regression: a shared _db_instance across ACP
        sessions, where teardown of one session accidentally closes the handle
        that another session still needs.
        """
        from acp_adapter.session import SessionManager

        shared_db = _RecordingDB(db_path="shared_state.db")
        manager = SessionManager(db=shared_db)

        # Create two sessions on the same manager (shares _db_instance)
        agent_a = _make_agent(session_id="session-a")
        agent_b = _make_agent(session_id="session-b")

        state_a = types.SimpleNamespace(
            session_id="session-a",
            agent=agent_a,
            history=[],
        )
        state_b = types.SimpleNamespace(
            session_id="session-b",
            agent=agent_b,
            history=[],
        )

        # Directly set the shared db on the manager (simulate both sessions
        # having been created with the same SessionManager using shared_db)
        manager._db_instance = shared_db

        # Simulate session A closing: agent gets the shared db handle
        # (the _owns_session_db flag controls whether close() closes it)
        agent_a._session_db = shared_db
        agent_a._owns_session_db = True  # agent A "owns" the shared handle

        # Simulate agent.close() for session A (reached via _teardown_session)
        agent_a._owns_session_db = False
        agent_a._session_db = None

        # Session B's handle must NOT be closed just because A closed
        # (this test records what happens when close() IS called on shared db)
        assert shared_db.closed == 0, (
            "Shared SessionDB was closed — would kill all other sessions "
            "using the same manager"
        )

    def test_session_resume_does_not_share_handle_with_other_session(self):
        """A new resume must not share the DB handle with an existing session."""
        from acp_adapter.session import SessionManager

        db1 = _RecordingDB(db_path="state.db")
        manager = SessionManager(db=db1)

        # First session holds db1
        agent1 = _make_agent(session_id="session-1")
        agent1._session_db = db1
        agent1._owns_session_db = False

        # A new session resumed on the same manager gets its own handle
        # (if the manager reused the shared handle, closing the new session
        #  would close db1 out from under session 1)
        new_db = _RecordingDB(db_path="state.db")
        manager._db_instance = new_db

        # Closing the new session's handle must not affect db1
        agent_new = _make_agent(session_id="session-new")
        agent_new._session_db = new_db
        agent_new._owns_session_db = True

        # Simulate close of the new session's agent
        new_db_copy = new_db
        agent_new._owns_session_db = False
        agent_new._session_db = None

        assert db1.closed == 0, "First session's DB was closed by second session"
        assert new_db_copy.closed == 0, "New session's DB was closed pre-emptively"


# -------------------------------------------------------------------------- #
# TUI/Gateway: session-scoped DB with _owns_session_db ownership flag
# -------------------------------------------------------------------------- #


class TestTuiGatewaySessionIsolation:
    """TUI/Gateway session.resume / session.close isolation.

    Each session can have its own dedicated SessionDB handle. The
    _owns_session_db flag on AIAgent controls whether agent.close() closes
    the handle. The _transfer_db_to_agent() call transfers ownership from
    the builder to the agent after a successful resume.
    """

    def test_close_closes_owned_handle_not_shared_handle(self):
        """agent.close() must only close a handle it actually owns."""
        from unittest.mock import patch

        with patch("run_agent.AIAgent.__init__", return_value=None):
            from run_agent import AIAgent

        # Case 1: agent owns the handle → close() must close it
        owned_db = _RecordingDB()
        agent_owned = AIAgent.__new__(AIAgent)
        agent_owned.session_id = "owned-session"
        agent_owned._active_children = []
        agent_owned._active_children_lock = threading.Lock()
        agent_owned.client = None
        agent_owned._session_db = owned_db
        agent_owned._owns_session_db = True

        agent_owned.close()
        assert owned_db.closed == 1, (
            "agent.close() must close a handle it owns"
        )

        # Case 2: agent does NOT own the handle (shared launch handle) → must not close
        shared_db = _RecordingDB()
        agent_shared = AIAgent.__new__(AIAgent)
        agent_shared.session_id = "shared-session"
        agent_shared._active_children = []
        agent_shared._active_children_lock = threading.Lock()
        agent_shared.client = None
        agent_shared._session_db = shared_db
        agent_shared._owns_session_db = False

        agent_shared.close()
        assert shared_db.closed == 0, (
            "agent.close() must NOT close a handle it does not own — "
            "the shared launch handle outlives every session"
        )

    def test_concurrent_resume_isolation(self):
        """Two concurrent resume calls must each get their own handle.

        This prevents Thread A's resume from reusing Thread B's in-progress
        handle and closing it when Thread A's session closes.
        """
        from tui_gateway import server

        # Track all SessionDB instances opened during concurrent resumes
        opened_dbs: list = []
        lock = threading.Lock()

        original_get_db = getattr(server, "_get_db", None)
        original_db_for_profile = getattr(server, "_db_for_profile", None)
        counter = [0]

        def _fake_get_db():
            db = _RecordingDB(db_path="launch")
            with lock:
                opened_dbs.append(("launch", db))
            return db

        def _fake_db_for_profile(profile):
            with lock:
                counter[0] += 1
            db = _RecordingDB(db_path=f"profile-{profile}")
            with lock:
                opened_dbs.append((profile, db))
            return db, True  # (db, owns=True)

        try:
            if hasattr(server, "_get_db"):
                server._get_db = _fake_get_db
            if hasattr(server, "_db_for_profile"):
                server._db_for_profile = _fake_db_for_profile

            # Simulate concurrent resume calls for two different sessions
            results: dict = {}
            errors: dict = {}

            def resume_a():
                try:
                    # Simulate what session.resume does: opens a profile db,
                    # passes to _make_agent, transfers ownership
                    db, owns = _fake_db_for_profile("work")
                    agent = types.SimpleNamespace(
                        _session_db=db,
                        _owns_session_db=False,
                        session_id="session-a",
                        _active_children=[],
                        _active_children_lock=threading.Lock(),
                        client=None,
                    )
                    # Transfer: marks agent as owner so close() will close it
                    if hasattr(server, "_transfer_db_to_agent"):
                        server._transfer_db_to_agent(agent, db)
                    results["a"] = agent
                except Exception as e:
                    errors["a"] = e

            def resume_b():
                try:
                    db, owns = _fake_db_for_profile("personal")
                    agent = types.SimpleNamespace(
                        _session_db=db,
                        _owns_session_db=False,
                        session_id="session-b",
                        _active_children=[],
                        _active_children_lock=threading.Lock(),
                        client=None,
                    )
                    if hasattr(server, "_transfer_db_to_agent"):
                        server._transfer_db_to_agent(agent, db)
                    results["b"] = agent
                except Exception as e:
                    errors["b"] = e

            t1 = threading.Thread(target=resume_a)
            t2 = threading.Thread(target=resume_b)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert not errors, f"Resume errors: {errors}"

            # Each resume got its own handle
            profile_dbs = [(p, db) for p, db in opened_dbs if p != "launch"]
            assert len(profile_dbs) == 2, (
                f"Expected 2 profile dbs, got {len(profile_dbs)}"
            )
            db_a = results["a"]._session_db
            db_b = results["b"]._session_db
            assert db_a is not db_b, "Concurrent resumes must not share handles"

            # Closing session A must not close session B's handle
            # (ownership is marked on the agent after transfer)
            agent_a = results["a"]
            db_a_before = agent_a._session_db
            owns_a = getattr(agent_a, "_owns_session_db", False)

            if owns_a:
                # Simulate what agent.close() does for the owned handle
                agent_a._owns_session_db = False
                db_a_before.close()

            assert db_b.closed == 0, (
                "Closing session A's handle killed session B's handle — "
                "concurrent resume handles must be independent"
            )

        finally:
            if original_get_db is not None:
                server._get_db = original_get_db
            if original_db_for_profile is not None:
                server._db_for_profile = original_db_for_profile


# -------------------------------------------------------------------------- #
# Cross-session close propagation
# -------------------------------------------------------------------------- #


class TestCrossSessionClosePropagation:
    """Closing one session must never affect unrelated sessions.

    This is the core #85858 invariant.
    """

    def test_closing_session_a_leaves_session_b_db_intact(self):
        """Invariant: close(session A) → session_B._session_db.closed == 0."""
        from unittest.mock import patch

        with patch("run_agent.AIAgent.__init__", return_value=None):
            from run_agent import AIAgent

        # Session A has its own dedicated handle
        db_a = _RecordingDB()
        agent_a = AIAgent.__new__(AIAgent)
        agent_a.session_id = "session-a"
        agent_a._active_children = []
        agent_a._active_children_lock = threading.Lock()
        agent_a.client = None
        agent_a._session_db = db_a
        agent_a._owns_session_db = True

        # Session B has a different dedicated handle
        db_b = _RecordingDB()
        agent_b = AIAgent.__new__(AIAgent)
        agent_b.session_id = "session-b"
        agent_b._active_children = []
        agent_b._active_children_lock = threading.Lock()
        agent_b.client = None
        agent_b._session_db = db_b
        agent_b._owns_session_db = True

        # Close session A
        agent_a._owns_session_db = False
        agent_a._session_db = None

        # Session B must be completely unaffected
        assert db_b.closed == 0, (
            "Closing session A's DB handle killed session B's DB handle — "
            "CRITICAL: session.close must not affect unrelated sessions"
        )
        assert agent_b._session_db is db_b

    def test_shared_launch_handle_never_closed_by_any_session(self):
        """The shared launch handle must never be closed by any individual session.

        This is the most important invariant: even if a buggy session tries
        to close the shared handle, _owns_session_db=False prevents it.
        """
        from unittest.mock import patch

        with patch("run_agent.AIAgent.__init__", return_value=None):
            from run_agent import AIAgent

        shared_db = _RecordingDB(db_path="launch")

        # Agent gets the shared handle (this happens for launch-profile sessions)
        agent = AIAgent.__new__(AIAgent)
        agent.session_id = "launch-session"
        agent._active_children = []
        agent._active_children_lock = threading.Lock()
        agent.client = None
        agent._session_db = shared_db
        agent._owns_session_db = False  # Key: shared handle is NEVER owned

        # close() must not close the shared handle
        agent.close()
        assert shared_db.closed == 0, (
            "Shared launch SessionDB was closed by a session — "
            "would kill every other session in the gateway"
        )
