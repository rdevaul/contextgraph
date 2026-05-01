"""
Test 3 — Global scope escape hatch.

Setup:
  - Multiple sessions across the same user.
  - Some rows in each.

Expected:
  - scope='global' returns cross-session, cross-channel results (no filtering).
  - scope='session' returns ONLY the requested session.
  - scope='user' returns ONLY the requested channel_label (cross-session within user).

Confirms that future tooling needing the wide view (research, analytics, audit)
can opt in via scope='global' without modifying the assembler.

References:
  - Bus thread:    20260501213940-5b002851
  - Approval:      20260501220916-a4feb6f0
"""
from __future__ import annotations

import time

import pytest

from _harness import fresh_store, insert, make_assembler


def _seed():
    store, _path = fresh_store()
    base = time.time() - 3600

    # Three users × two sessions × three rows each = 18 rows, all tagged 'topic-x'.
    for user in ("garrett", "rich", "jeremy"):
        for s in ("a", "b"):
            for i in range(3):
                insert(
                    store,
                    msg_id=f"{user}-{s}-{i}",
                    user_text=f"{user}/{s} turn {i}",
                    assistant_text="content",
                    tags=["topic-x"],
                    channel_label=user,
                    session_id=f"{user}-session-{s}",
                    timestamp=base + (hash((user, s, i)) % 100),
                )

    return store


def test_global_scope_returns_cross_session_results():
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="any topic-x stuff",
        inferred_tags=["topic-x"],
        channel_label=None,
        session_id=None,
        scope="global",
    )

    seen_labels = {m.channel_label for m in result.messages}
    seen_sessions = {m.session_id for m in result.messages}
    print(f"[global] retrieved={len(result.messages)} labels={seen_labels} "
          f"#sessions={len(seen_sessions)}")
    assert len(seen_labels) >= 2, (
        f"global scope should return rows from multiple users, got labels={seen_labels}"
    )
    assert len(seen_sessions) >= 2, (
        f"global scope should return rows from multiple sessions, got {seen_sessions}"
    )


def test_session_scope_filters_to_one_session():
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    target = "garrett-session-a"
    result = asm.assemble(
        incoming_text="topic-x in one session",
        inferred_tags=["topic-x"],
        channel_label="garrett",
        session_id=target,
        scope="session",
    )

    seen_sessions = {m.session_id for m in result.messages}
    print(f"[session={target}] retrieved={len(result.messages)} sessions={seen_sessions}")
    assert seen_sessions == {target}, (
        f"session scope should return only target session, got {seen_sessions}"
    )


def test_user_scope_filters_to_one_channel():
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="topic-x for one user",
        inferred_tags=["topic-x"],
        channel_label="rich",
        session_id=None,
        scope="user",
    )

    seen_labels = {m.channel_label for m in result.messages}
    seen_sessions = {m.session_id for m in result.messages}
    print(f"[user=rich] retrieved={len(result.messages)} labels={seen_labels} sessions={seen_sessions}")
    assert seen_labels == {"rich"}, f"user scope should be rich-only, got {seen_labels}"
    # Cross-session within user is intentional under scope='user'.
    assert len(seen_sessions) >= 2, (
        f"user scope should keep cross-session continuity for same user, got {seen_sessions}"
    )


def test_invalid_scope_raises():
    store = _seed()
    asm = make_assembler(store, token_budget=4000)
    with pytest.raises(ValueError):
        asm.assemble(
            incoming_text="x",
            inferred_tags=["topic-x"],
            channel_label="garrett",
            session_id="garrett-session-a",
            scope="bogus",  # type: ignore[arg-type]
        )


if __name__ == "__main__":
    print("=" * 70)
    print("Test 3 — global scope escape hatch + session/user filters")
    print("=" * 70)
    test_global_scope_returns_cross_session_results()
    print("✓ test_global_scope_returns_cross_session_results")
    test_session_scope_filters_to_one_session()
    print("✓ test_session_scope_filters_to_one_session")
    test_user_scope_filters_to_one_channel()
    print("✓ test_user_scope_filters_to_one_channel")
    try:
        test_invalid_scope_raises()
        print("✓ test_invalid_scope_raises")
    except Exception as e:
        print(f"✗ test_invalid_scope_raises: {e}")
        raise
    print("PASS")
