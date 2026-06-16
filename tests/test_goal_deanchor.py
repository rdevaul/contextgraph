"""
test_goal_deanchor.py — Unit tests for the goal-watcher de-anchoring fix
(2026-06-15). See projects/current-thing/DIAGNOSIS-stale-goal-anchoring-2026-06-15.md
and IMPL-NOTES-deanchor-2026-06-15.md.

These are deterministic: _call_llm is never hit; canned canonical/legacy JSON is
fed through _process_watcher_response + _apply_drift_result against a temp SQLite
DB. No live model, no running server required.

Covers:
  (a) regression: a stored STALE goal the conversation has moved past gets
      FLIPPED, not re-stamped (the actual bug).
  (b) a genuinely-unchanged goal stays put (no false flip).
  (c) the staleness ceiling forces re-inference.
  (d) changed_at only updates on a real primary change (not on re-stamps).
  plus: superseded immediate flip, immediate-flip on low overlap, backward-compat
  parsing of the legacy anchored response shape, two-stage prompt de-anchoring.
"""

import os
import time
import importlib

import pytest


@pytest.fixture()
def ct_env(tmp_path, monkeypatch):
    """Isolated DB + fresh module state for each test."""
    db = tmp_path / "store.db"
    monkeypatch.setenv("CONTEXTGRAPH_DB_PATH", str(db))
    monkeypatch.setenv("CONTEXT_CURRENT_THING_ENABLED", "1")
    monkeypatch.setenv("GOAL_WATCHER_DEANCHOR", "1")

    import api.current_thing as ct
    import api.goal_watcher as gw
    importlib.reload(ct)
    importlib.reload(gw)
    ct.ensure_tables()

    # Clear per-session module state.
    gw._major_votes.clear()
    gw._turns_since_change.clear()
    return ct, gw


def _seed_goal(ct, session_id, primary, active, *, changed_at=None, source="llm",
               confidence=0.6, locked=False):
    """Create a snapshot with a stored goal."""
    snap = ct.CurrentThingSnapshot()
    snap.session_id = session_id
    snap.pane_label = "test-pane"
    snap.goals.primary = primary
    snap.goals.active = active
    snap.goals.source = source
    snap.goals.confidence = confidence
    snap.goals.locked_by_user = locked
    snap.goals.changed_at = time.time() if changed_at is None else changed_at
    ct.save_snapshot(snap, change_reason="test-seed")
    return snap


def _event(gw, session_id, user_text, primary, active, recent_turns=None,
           changed_at=0.0):
    return gw.WatcherEvent(
        session_id=session_id,
        pane_label="test-pane",
        user="Rich",
        user_text=user_text,
        recent_turns=recent_turns or [],
        current_primary_goal=primary,
        current_active=active,
        current_changed_at=changed_at,
    )


# ── (a) REGRESSION: stale goal the conversation moved past gets FLIPPED ────────

def test_stale_goal_flips_on_superseded(ct_env):
    ct, gw = ct_env
    sid = "agent:test:stale-superseded"
    stale = "troubleshoot and develop the diffusion-based routing system"
    _seed_goal(ct, sid, stale, ["Refine routing diffusion model"],
               changed_at=time.time() - 5 * 86400)  # 5 days old

    # The model, anchor-free, re-derives the real current goal and marks the
    # stored one superseded.
    canned = {
        "current_goal": "Fix contextgraph stale goal anchoring and ship the rehydrate spec",
        "active_sub_goals": ["Investigate context graph pollution", "Spawn rehydrate sub-agent"],
        "completed": ["Backed up DB", "Installed plugin"],
        "comparison": "superseded",
        "evidence": "conversation is entirely about contextgraph leaks, not routing",
    }
    result = gw._process_watcher_response(sid, __import__("json").dumps(canned))
    ev = _event(gw, sid, "the current thing is wrong, it still says diffusion routing", stale,
                ["Refine routing diffusion model"], changed_at=time.time() - 5 * 86400)
    gw._apply_drift_result(sid, result, ev)

    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == canned["current_goal"], "stale goal should be FLIPPED, not re-stamped"
    assert snap.goals.source == "llm"


def test_stale_goal_flips_on_drift_low_overlap(ct_env):
    """Even without 'superseded', a single 'drifted' verdict with near-zero
    stored↔conversation overlap should flip immediately (fix #3)."""
    ct, gw = ct_env
    sid = "agent:test:stale-driftflip"
    stale = "optimize the diffusion-based routing system parameters"
    _seed_goal(ct, sid, stale, ["tune routing"], changed_at=time.time() - 3 * 86400)

    canned = {
        "current_goal": "Audit firewall rules and harden SSH on the lab VPS",
        "active_sub_goals": ["review ufw rules", "disable password auth"],
        "completed": [],
        "comparison": "drifted",
        "evidence": "topic is now host security hardening",
    }
    result = gw._process_watcher_response(sid, __import__("json").dumps(canned))
    ev = _event(
        gw, sid,
        "let's audit the firewall and harden ssh on the vps, check ufw and fail2ban",
        stale, ["tune routing"],
        recent_turns=[{"user": "what's our ssh exposure", "assistant": "let me check ufw and sshd_config"}],
        changed_at=time.time() - 3 * 86400,
    )
    gw._apply_drift_result(sid, result, ev)
    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == canned["current_goal"]


# ── (b) genuinely-unchanged goal stays put (no false flip) ────────────────────

def test_unchanged_goal_stays_put(ct_env):
    ct, gw = ct_env
    sid = "agent:test:nochange"
    goal = "Implement the contextgraph rehydrate spec for multigraph panes"
    seeded = _seed_goal(ct, sid, goal,
                        ["wire up rehydrate endpoint", "add pane refresh"],
                        changed_at=time.time())  # fresh

    canned = {
        "current_goal": "Implement the rehydrate spec for multigraph panes",
        "active_sub_goals": ["wire up rehydrate endpoint", "add pane refresh"],
        "completed": ["scoped the endpoint"],
        "comparison": "match",
        "evidence": "",
    }
    result = gw._process_watcher_response(sid, __import__("json").dumps(canned))
    ev = _event(gw, sid,
                "ok now let's wire up the rehydrate endpoint and add the pane refresh",
                goal, ["wire up rehydrate endpoint", "add pane refresh"],
                changed_at=seeded.goals.changed_at)
    gw._apply_drift_result(sid, result, ev)

    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == goal, "matching goal must not flip"


def test_single_drifted_vote_does_not_flip_when_overlap_high(ct_env):
    """Ambiguous drift (related goal, decent overlap, fresh) needs 2 votes —
    one 'drifted' alone must not flip primary."""
    ct, gw = ct_env
    sid = "agent:test:ambiguous"
    goal = "Improve the contextgraph retrieval quality for recent turns"
    _seed_goal(ct, sid, goal, ["tune recency floor"], changed_at=time.time())

    canned = {
        "current_goal": "Improve contextgraph retrieval recency floor and quality scoring",
        "active_sub_goals": ["tune recency floor", "add quality scoring"],
        "completed": [],
        "comparison": "drifted",
        "evidence": "related refinement of the same retrieval goal",
    }
    result = gw._process_watcher_response(sid, __import__("json").dumps(canned))
    ev = _event(
        gw, sid,
        "let's also improve the contextgraph retrieval quality and recency scoring",
        goal, ["tune recency floor"],
        recent_turns=[{"user": "the recency floor for contextgraph retrieval", "assistant": "tuning quality"}],
        changed_at=time.time(),
    )
    gw._apply_drift_result(sid, result, ev)
    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == goal, "one ambiguous drift vote should NOT flip"
    assert gw._major_votes.get(sid) == 1


def test_two_drifted_votes_flip(ct_env):
    """Two consecutive ambiguous 'drifted' verdicts ratify a flip."""
    ct, gw = ct_env
    sid = "agent:test:twovote"
    goal = "Improve the contextgraph retrieval quality for recent turns"
    _seed_goal(ct, sid, goal, ["tune recency floor"], changed_at=time.time())

    canned = {
        "current_goal": "Improve contextgraph retrieval recency floor and quality scoring",
        "active_sub_goals": ["tune recency floor", "add quality scoring"],
        "completed": [],
        "comparison": "drifted",
        "evidence": "related refinement",
    }
    js = __import__("json").dumps(canned)
    ev = _event(
        gw, sid,
        "let's also improve the contextgraph retrieval quality and recency scoring here",
        goal, ["tune recency floor"],
        recent_turns=[{"user": "the recency floor for contextgraph retrieval quality", "assistant": "ok"}],
        changed_at=time.time(),
    )
    gw._apply_drift_result(sid, gw._process_watcher_response(sid, js), ev)
    gw._apply_drift_result(sid, gw._process_watcher_response(sid, js), ev)
    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == canned["current_goal"], "second drift vote should ratify"


# ── (c) staleness ceiling forces re-inference ─────────────────────────────────

def test_staleness_ceiling_forces_reinference(ct_env, monkeypatch):
    """A 'match' verdict on a goal that's old (turns ceiling) AND diverges from
    the re-derivation should still force a re-inference (fix #2)."""
    ct, gw = ct_env
    monkeypatch.setenv("GOAL_WATCHER_STALENESS_TURNS", "3")
    importlib.reload(gw)
    gw._major_votes.clear()
    gw._turns_since_change.clear()

    sid = "agent:test:staleceiling"
    stale = "develop the diffusion-based routing system architecture"
    _seed_goal(ct, sid, stale, ["routing arch"], changed_at=time.time() - 10 * 86400)

    # Model insists 'match' (anchoring-style) but re-derives a divergent goal.
    canned = {
        "current_goal": "Harden lab host security and audit firewall exposure",
        "active_sub_goals": ["audit ufw", "ssh hardening"],
        "completed": [],
        "comparison": "match",  # model wrongly says match
        "evidence": "",
    }
    result = gw._process_watcher_response(sid, __import__("json").dumps(canned))
    ev = _event(gw, sid,
                "audit the firewall exposure and harden ssh on the host please now",
                stale, ["routing arch"],
                changed_at=time.time() - 10 * 86400)

    # Push the turn counter past the ceiling.
    gw._turns_since_change[sid] = 5
    gw._apply_drift_result(sid, result, ev)

    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == canned["current_goal"], \
        "stale + divergent should force re-inference even on a 'match' vote"


def test_staleness_ceiling_respects_overlap(ct_env, monkeypatch):
    """If the re-derived goal still overlaps the stored goal, staleness must NOT
    force a flip (no false positives from age alone)."""
    ct, gw = ct_env
    monkeypatch.setenv("GOAL_WATCHER_STALENESS_TURNS", "3")
    importlib.reload(gw)
    gw._major_votes.clear()
    gw._turns_since_change.clear()

    sid = "agent:test:staleoverlap"
    goal = "develop the contextgraph rehydrate spec and pane refresh"
    _seed_goal(ct, sid, goal, ["rehydrate"], changed_at=time.time() - 10 * 86400)

    canned = {
        "current_goal": "develop the contextgraph rehydrate spec pane refresh logic",
        "active_sub_goals": ["rehydrate", "pane refresh"],
        "completed": [],
        "comparison": "match",
        "evidence": "",
    }
    result = gw._process_watcher_response(sid, __import__("json").dumps(canned))
    ev = _event(gw, sid,
                "keep going on the contextgraph rehydrate spec and pane refresh logic here",
                goal, ["rehydrate"],
                changed_at=time.time() - 10 * 86400)
    gw._turns_since_change[sid] = 5
    gw._apply_drift_result(sid, result, ev)
    snap = ct.load_snapshot(sid)
    assert snap.goals.primary == goal, "high overlap → no stale flip"


# ── (d) changed_at only updates on a real primary change ──────────────────────

def test_changed_at_updates_only_on_real_change(ct_env):
    ct, gw = ct_env
    sid = "agent:test:changedat"
    t0 = time.time() - 1000
    _seed_goal(ct, sid, "Goal A original", ["a"], changed_at=t0)

    # Re-stamp the SAME primary (whitespace/case variation) → changed_at unchanged.
    ct.update_snapshot_goals(sid, primary="  goal a original  ", source="llm")
    snap = ct.load_snapshot(sid)
    assert snap.goals.changed_at == pytest.approx(t0), \
        "re-stamping the same goal must NOT move changed_at"

    # Genuine change → changed_at advances.
    before = time.time()
    ct.update_snapshot_goals(sid, primary="Goal B completely different", source="llm")
    snap = ct.load_snapshot(sid)
    assert snap.goals.changed_at >= before, "real primary change must update changed_at"
    assert snap.goals.primary == "Goal B completely different"


def test_changed_at_not_touched_on_status_only_update(ct_env):
    """A status/confidence-only update (no primary) must not move changed_at."""
    ct, gw = ct_env
    sid = "agent:test:changedat-status"
    t0 = time.time() - 500
    _seed_goal(ct, sid, "Stable goal", ["x"], changed_at=t0)
    ct.update_snapshot_goals(sid, confidence=0.4, watcher_status="idle")
    snap = ct.load_snapshot(sid)
    assert snap.goals.changed_at == pytest.approx(t0)


# ── parser backward-compat ────────────────────────────────────────────────────

def test_legacy_anchored_response_parses(ct_env):
    """Old anchored JSON shape still normalizes into the canonical result."""
    ct, gw = ct_env
    legacy = {
        "goal_changed": True,
        "new_primary_goal": "Build the new feed system router",
        "active_sub_goals": ["route feedlines"],
        "completed": [],
        "drift_severity": "major",
        "evidence": "clear topic change",
    }
    res = gw._process_watcher_response("sid", __import__("json").dumps(legacy))
    assert res["comparison"] == "superseded"
    assert res["current_goal"] == "Build the new feed system router"


def test_legacy_no_drift_parses_as_match(ct_env):
    ct, gw = ct_env
    legacy = {
        "goal_changed": False, "new_primary_goal": "",
        "active_sub_goals": ["x"], "completed": [],
        "drift_severity": "none", "evidence": "",
    }
    res = gw._process_watcher_response("sid", __import__("json").dumps(legacy))
    assert res["comparison"] == "match"


def test_garbage_response_defaults_to_match(ct_env):
    ct, gw = ct_env
    res = gw._process_watcher_response("sid", "not json at all {")
    assert res["comparison"] == "match"
    assert res["current_goal"] == ""


# ── prompt de-anchoring ───────────────────────────────────────────────────────

def test_deanchored_prompt_omits_anchor_before_stage1(ct_env):
    """The stored goal must appear only in STAGE 2, after the model is asked to
    derive the goal from the conversation alone."""
    ct, gw = ct_env
    ev = _event(gw, "sid", "audit the firewall now",
                "develop diffusion routing system", ["routing"])
    p = gw._build_prompt(ev)
    assert "STAGE 1" in p and "STAGE 2" in p
    i_stage1 = p.index("STAGE 1")
    i_stage2 = p.index("STAGE 2")
    i_stored = p.index("develop diffusion routing system")
    assert i_stage1 < i_stage2 < i_stored, "stored goal must come AFTER stage 1 (de-anchored)"


def test_legacy_prompt_used_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTGRAPH_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setenv("GOAL_WATCHER_DEANCHOR", "0")
    import api.goal_watcher as gw
    importlib.reload(gw)
    ev = gw.WatcherEvent(
        session_id="sid", pane_label="p", user="Rich",
        user_text="hello there", recent_turns=[],
        current_primary_goal="old goal", current_active=[],
    )
    p = gw._build_prompt(ev)
    assert "Last known primary goal" in p, "flag off → legacy anchored prompt"
