"""
Test 2 — Cross-user isolation.

Setup:
  - Stand up a 'rich' channel_label row alongside a corpus of 'garrett' rows.
  - Issue a 'garrett' query.

Expected (post Part A):
  - With scope='user' AND channel_label='garrett', the rich row does NOT bleed in.
  - With scope='global', it does (proving the fix is the gate).

Mirrors the production scenario where 686 garrett rows + a future rich row
must not cross over.

References:
  - Bus thread:    20260501213940-5b002851
  - Approval:      20260501220916-a4feb6f0
"""
from __future__ import annotations

import time

from _harness import fresh_store, insert, make_assembler


def _seed():
    store, _path = fresh_store()
    base = time.time() - 3600

    # 10 garrett rows tagged 'rocket-design'
    for i in range(10):
        insert(
            store,
            msg_id=f"garrett-{i}",
            user_text=f"garrett turn {i}",
            assistant_text="something about thrust structure",
            tags=["rocket-design", "fea"],
            channel_label="garrett",
            session_id=f"garrett-session-{i}",
            timestamp=base + i,
        )

    # 1 rich row with the SAME tag — this is the canary that must not bleed.
    insert(
        store,
        msg_id="rich-secret-1",
        user_text="rich's private design note",
        assistant_text="Don't share with garrett — different program.",
        tags=["rocket-design", "fea"],
        channel_label="rich",
        session_id="rich-session-1",
        timestamp=base + 100,
    )

    return store


def test_user_scope_blocks_cross_channel_bleed():
    """Garrett's user-scoped query must not pull rich's row."""
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="rocket-design query as garrett",
        inferred_tags=["rocket-design"],
        channel_label="garrett",
        session_id="garrett-session-9",
        scope="user",
    )

    seen_labels = {m.channel_label for m in result.messages}
    rich_ids = [m.id for m in result.messages if m.channel_label == "rich"]
    print(f"[user/garrett] retrieved={len(result.messages)} labels={seen_labels} rich_rows={rich_ids}")
    assert "garrett" in seen_labels, "expected garrett's own rows"
    assert "rich" not in seen_labels, (
        f"CROSS-USER LEAK: rich row(s) {rich_ids} bled into garrett's user-scope query"
    )


def test_global_scope_does_show_cross_channel():
    """Sanity: in scope='global', the row IS visible — proves the test data is correct."""
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="rocket-design query global",
        inferred_tags=["rocket-design"],
        channel_label="garrett",  # ignored in global scope
        session_id="garrett-session-9",
        scope="global",
    )

    seen_labels = {m.channel_label for m in result.messages}
    print(f"[global] retrieved={len(result.messages)} labels={seen_labels}")
    assert "rich" in seen_labels, (
        "global scope should not filter; rich row should be reachable. "
        "If this fails, the test data isn't proving anything."
    )


def test_user_scope_with_rich_label_sees_only_rich():
    """Symmetric check: rich's user-scope query sees rich rows, not garrett's."""
    store = _seed()
    asm = make_assembler(store, token_budget=4000)

    result = asm.assemble(
        incoming_text="rocket-design query as rich",
        inferred_tags=["rocket-design"],
        channel_label="rich",
        session_id="rich-session-1",
        scope="user",
    )

    seen_labels = {m.channel_label for m in result.messages}
    print(f"[user/rich] retrieved={len(result.messages)} labels={seen_labels}")
    assert seen_labels == {"rich"}, f"rich scope should be rich-only, got {seen_labels}"


if __name__ == "__main__":
    print("=" * 70)
    print("Test 2 — cross-user isolation")
    print("=" * 70)
    test_user_scope_blocks_cross_channel_bleed()
    print("✓ test_user_scope_blocks_cross_channel_bleed")
    test_global_scope_does_show_cross_channel()
    print("✓ test_global_scope_does_show_cross_channel")
    test_user_scope_with_rich_label_sees_only_rich()
    print("✓ test_user_scope_with_rich_label_sees_only_rich")
    print("PASS")
