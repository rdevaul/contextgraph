"""
Test 1 — Repro the original symptom.

Setup:
  - Pane A and Pane B share a user (channel_label='garrett') but are different
    sessions. Both pane B and pane A have ingested rows tagged 'agentic-1'.
  - Pane A asks a fresh question with the same tag.

Expected (post Part A + Part B):
  - With scope='session', pane A retrieves ONLY its own rows for that tag.
  - With scope='global' (legacy), pane A retrieves rows from BOTH panes (proves
    the legacy behavior is what was leaking, and that the fix actually fires).

Captured: before/after retrieval counts.

References:
  - Bus thread:    20260501213940-5b002851
  - Approval:      20260501220916-a4feb6f0
  - Forensic note: agentic-1-assembly-FORENSICS-2026-05-01.md
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


def test_user_scope_keeps_cross_pane_for_same_user():
    """Default 'user' scope intentionally allows cross-pane retrieval for the same user.

    This preserves Discord-DM-style continuity. It is NOT the multigraph default
    (multigraph panes use scope='session'), but external tools can opt in.
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
    # Same user, different panes — both should be reachable in 'user' scope.
    assert {PANE_A, PANE_B}.issubset(seen_sessions), (
        f"user-scope should keep cross-pane continuity for same user, got {seen_sessions}"
    )


if __name__ == "__main__":
    print("=" * 70)
    print("Test 1 — repro original symptom")
    print("=" * 70)
    test_global_scope_leaks_pane_b_into_pane_a()
    print("✓ test_global_scope_leaks_pane_b_into_pane_a")
    test_session_scope_isolates_pane_a()
    print("✓ test_session_scope_isolates_pane_a")
    test_user_scope_keeps_cross_pane_for_same_user()
    print("✓ test_user_scope_keeps_cross_pane_for_same_user")
    print("PASS")
