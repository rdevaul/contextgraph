"""
Test 1 — Repro the original symptom.

Setup:
  - Pane A and Pane B share a user (channel_label='garrett') but are different
    sessions. Both pane B and pane A have ingested rows tagged 'agentic-1'.
  - Pane A asks a fresh question with the same tag.

Expected (current policy, post 2026-06-09 dashboard-exclusion):
  - scope='session': pane A retrieves ONLY its own rows for that tag.
  - scope='global' (legacy): pane A retrieves rows from BOTH panes (proves the
    legacy behavior is what was leaking, and that the fix actually fires).
  - scope='user': ':dashboard:' panes are EXCLUDED even for the same user
    (2026-06-09 policy). Non-dashboard same-user sessions still share context.

Captured: before/after retrieval counts.

References:
  - Bus thread:    20260501213940-5b002851
  - Approval:      20260501220916-a4feb6f0
  - Forensic note: agentic-1-assembly-FORENSICS-2026-05-01.md
  - Policy change: assembler.py get_recent_by_channel(exclude_dashboard=True),
    get_by_tag_scoped(exclude_dashboard=True) under scope=='user' (2026-06-09)
"""
from __future__ import annotations

import time

from _harness import fresh_store, insert, make_assembler


PANE_A = "agent:jarvis-garrett:dashboard:pane-a-uuid"
PANE_B = "agent:jarvis-garrett:dashboard:pane-b-uuid"
USER = "garrett"


def _seed():
    store, _path = fresh_store()
    base = time.time() - 3600  # 1h ago

    # Pane B's prior context — five turns talking about agentic-1 nosecone work.
    for i in range(5):
        insert(
            store,
            msg_id=f"pane-b-{i}",
            user_text=f"pane B turn {i}: nosecone trim radius",
            assistant_text="adjusted forward fairing datum",
            tags=["agentic-1", "rocket-design"],
            channel_label=USER,
            session_id=PANE_B,
            timestamp=base + i,
        )

    # Pane A's earlier context — three turns on agentic-1 fins.
    for i in range(3):
        insert(
            store,
            msg_id=f"pane-a-{i}",
            user_text=f"pane A turn {i}: fin fillet sweep",
            assistant_text="canted fin tip 0.5deg",
            tags=["agentic-1", "rocket-design"],
            channel_label=USER,
            session_id=PANE_A,
            timestamp=base + 100 + i,
        )

    return store


def test_global_scope_leaks_pane_b_into_pane_a():
    """Pre-fix legacy behavior — confirms the bug existed."""
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="what's the latest on the agentic-1 design",
        inferred_tags=["agentic-1"],
        channel_label=USER,
        session_id=PANE_A,
        scope="global",
    )

    seen_sessions = {m.session_id for m in result.messages}
    print(f"[global] retrieved={len(result.messages)} sessions={seen_sessions}")
    # Legacy/global behavior should pull from BOTH panes.
    assert PANE_A in seen_sessions, "expected pane A's own rows in global scope"
    assert PANE_B in seen_sessions, "expected pane B's rows in global scope (the leak)"


def test_session_scope_isolates_pane_a():
    """Post-fix Part B behavior — pane A no longer pulls pane B's rows."""
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="what's the latest on the agentic-1 design",
        inferred_tags=["agentic-1"],
        channel_label=USER,
        session_id=PANE_A,
        scope="session",
    )

    seen_sessions = {m.session_id for m in result.messages}
    print(f"[session] retrieved={len(result.messages)} sessions={seen_sessions}")
    assert PANE_A in seen_sessions, "expected pane A's own rows under session scope"
    assert PANE_B not in seen_sessions, (
        f"LEAK: pane A pulled pane B rows under session scope: {seen_sessions}"
    )


def test_user_scope_excludes_dashboard_panes_for_same_user():
    """Policy change 2026-06-09: ':dashboard:' panes are EXCLUDED from user-scope
    retrieval, even for the same user.

    Rationale (assembler.py, get_recent_by_channel(exclude_dashboard=True) +
    get_by_tag_scoped(exclude_dashboard=True) under scope=='user'):
      Multigraph pane work is pane-scoped. Letting one pane's rows vacuum into
      another pane's user-scope assembly is exactly the cross-pane bleed we
      fixed. Deliberate cross-pane continuity goes through assemble-time
      bridging (Current Thing), NOT through accidental recency/topic overlap.

    Both PANE_A and PANE_B here are ':dashboard:' sessions, so a user-scope
    query from PANE_A must surface NEITHER pane's rows via the
    recency/topic layers.

    See the live e2e check A2 in tests/e2e_multipane/run_e2e.py for the
    HTTP-level equivalent.
    """
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="what's the latest on the agentic-1 design",
        inferred_tags=["agentic-1"],
        channel_label=USER,
        session_id=PANE_A,
        scope="user",
    )

    seen_sessions = {m.session_id for m in result.messages}
    print(f"[user] retrieved={len(result.messages)} sessions={seen_sessions}")
    # Dashboard panes are excluded from user-scope — neither pane should appear.
    assert PANE_A not in seen_sessions and PANE_B not in seen_sessions, (
        f"user-scope must exclude :dashboard: panes, got {seen_sessions}"
    )


def test_user_scope_keeps_continuity_for_non_dashboard_sessions():
    """The continuity guarantee user-scope DOES still provide: two NON-dashboard
    sessions for the same user remain mutually reachable under scope='user'.

    This is the Discord-DM-style continuity the exclusion rule does not touch —
    only ':dashboard:' (Multigraph pane) sessions are filtered out.
    """
    store, _path = fresh_store()
    base = time.time() - 3600
    sess_x = "agent:jarvis-garrett:direct:garrett-dm"          # non-dashboard
    sess_y = "agent:jarvis-garrett:thread:garrett-thread-1"    # non-dashboard

    for i in range(4):
        insert(
            store, msg_id=f"x-{i}",
            user_text=f"DM turn {i}: nosecone trim radius",
            assistant_text="adjusted forward fairing datum",
            tags=["agentic-1", "rocket-design"],
            channel_label=USER, session_id=sess_x, timestamp=base + i,
        )
    for i in range(3):
        insert(
            store, msg_id=f"y-{i}",
            user_text=f"thread turn {i}: fin fillet sweep",
            assistant_text="canted fin tip 0.5deg",
            tags=["agentic-1", "rocket-design"],
            channel_label=USER, session_id=sess_y, timestamp=base + 100 + i,
        )

    asm = make_assembler(store, token_budget=4000)
    result = asm.assemble(
        incoming_text="what's the latest on the agentic-1 design",
        inferred_tags=["agentic-1"],
        channel_label=USER,
        session_id=sess_x,
        scope="user",
    )
    seen_sessions = {m.session_id for m in result.messages}
    print(f"[user/non-dash] retrieved={len(result.messages)} sessions={seen_sessions}")
    # Same user, non-dashboard sessions — cross-session continuity preserved.
    assert {sess_x, sess_y}.issubset(seen_sessions), (
        f"user-scope should keep continuity for non-dashboard sessions, got {seen_sessions}"
    )


if __name__ == "__main__":
    print("=" * 70)
    print("Test 1 — repro original symptom")
    print("=" * 70)
    test_global_scope_leaks_pane_b_into_pane_a()
    print("✓ test_global_scope_leaks_pane_b_into_pane_a")
    test_session_scope_isolates_pane_a()
    print("✓ test_session_scope_isolates_pane_a")
    test_user_scope_excludes_dashboard_panes_for_same_user()
    print("✓ test_user_scope_excludes_dashboard_panes_for_same_user")
    test_user_scope_keeps_continuity_for_non_dashboard_sessions()
    print("✓ test_user_scope_keeps_continuity_for_non_dashboard_sessions")
    print("PASS")
