"""
goal_watcher.py — Async parallel LLM goal-drift watcher.

Monitors conversation turns for goal drift and updates the Current Thing
snapshot's goal fields. Runs completely asynchronously — never blocks /assemble.

Model selection (Q2 decision, 2026-06-08):
  Default: qwen3-coder:30b on Ollama @ Mac Studio (172.23.1.31:11434)
  Escalation: DeepSeek-V4-Flash @ 172.23.1.31:11435 (flip GOAL_WATCHER_MODEL)
  Offline: watcher disables itself gracefully; snapshot keeps last-known goals

Feature flag: CONTEXT_CURRENT_THING_ENABLED=1 (shared with current_thing.py)
"""

import json
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

# Model endpoint. Flip GOAL_WATCHER_MODEL to escalate.
# Format: "ollama:<base_url>/<model>" or "openai-compat:<base_url>/<model>"
_WATCHER_MODEL_DEFAULT = "ollama:http://172.23.1.31:11434/qwen3-coder:30b"
GOAL_WATCHER_MODEL = os.environ.get("GOAL_WATCHER_MODEL", _WATCHER_MODEL_DEFAULT)

# Hard wall-clock budget per watcher call (seconds).
# 15s default: cold-loading qwen3-coder:30b on the Studio takes ~6s before
# generating; 5s timed out on every cold start (observed 2026-06-09). Warm
# calls answer in <1s, so this only affects the first call after model evict.
WATCHER_TIMEOUT_S = float(os.environ.get("GOAL_WATCHER_TIMEOUT_S", "15.0"))

# Minimum tokens in a user turn to bother running the watcher.
WATCHER_MIN_TOKENS = int(os.environ.get("GOAL_WATCHER_MIN_TOKENS", "50"))

# Max LLM calls per session per second (debounce).
WATCHER_DEBOUNCE_S = float(os.environ.get("GOAL_WATCHER_DEBOUNCE_S", "30.0"))

# Consecutive "major" drift votes required before triggering a major update.
MAJOR_DRIFT_THRESHOLD = int(os.environ.get("GOAL_WATCHER_MAJOR_THRESHOLD", "2"))

# ── De-anchor fix config (2026-06-15) ──────────────────────────────────────────
# See projects/current-thing/DIAGNOSIS-stale-goal-anchoring-2026-06-15.md

# Fix #1: De-anchored two-stage prompt. ON by default — it IS the fix. Set
# GOAL_WATCHER_DEANCHOR=0 to fall back to the legacy anchored prompt + parser.
GOAL_WATCHER_DEANCHOR = os.environ.get("GOAL_WATCHER_DEANCHOR", "1").strip() == "1"

# Fix #2: Hard staleness ceiling. When a goal's last GENUINE change
# (goals.changed_at) is older than EITHER threshold AND the anchor-free
# re-derivation diverges (token overlap < STALENESS_OVERLAP with stored),
# force a re-inference regardless of the minor/major vote.
# Disable the ceiling entirely with GOAL_WATCHER_STALENESS=0.
GOAL_WATCHER_STALENESS = os.environ.get("GOAL_WATCHER_STALENESS", "1").strip() == "1"
GOAL_WATCHER_STALENESS_TURNS = int(os.environ.get("GOAL_WATCHER_STALENESS_TURNS", "20"))
GOAL_WATCHER_STALENESS_SECONDS = float(os.environ.get("GOAL_WATCHER_STALENESS_SECONDS", "7200"))  # 2h
# Token-overlap below which the re-derived goal is considered "divergent"
# from the stored goal (staleness ceiling trigger).
GOAL_WATCHER_STALENESS_OVERLAP = float(os.environ.get("GOAL_WATCHER_STALENESS_OVERLAP", "0.3"))

# Fix #3: Single-verdict immediate flip. A lone `superseded`/`major` verdict
# with stored↔conversation token overlap below this threshold flips the goal
# immediately, bypassing the consecutive-vote threshold.
GOAL_WATCHER_IMMEDIATE_FLIP_OVERLAP = float(
    os.environ.get("GOAL_WATCHER_IMMEDIATE_FLIP_OVERLAP", "0.2")
)


# ── Watcher event ─────────────────────────────────────────────────────────────

@dataclass
class WatcherEvent:
    session_id: str
    pane_label: str
    user: str
    user_text: str                   # most-recent user turn
    recent_turns: list[dict]         # last 8 turns, newest first: [{user, assistant}]
    current_primary_goal: Optional[str]
    current_active: list[str]
    current_changed_at: float = 0.0   # goals.changed_at from snapshot (staleness clock)
    enqueued_at: float = 0.0

    def __post_init__(self):
        if self.enqueued_at == 0.0:
            self.enqueued_at = time.time()


# ── LLM client ────────────────────────────────────────────────────────────────

def _parse_model_spec(spec: str) -> tuple[str, str, str]:
    """
    Parse "ollama:<base_url>/<model>" or "openai-compat:<base_url>/<model>".
    Returns (backend, base_url, model_name).
    """
    if spec.startswith("ollama:"):
        rest = spec[len("ollama:"):]
    elif spec.startswith("openai-compat:"):
        rest = spec[len("openai-compat:"):]
    else:
        rest = spec

    # Split off model name (last path segment after the port)
    # e.g. http://172.23.1.31:11434/qwen3-coder:30b
    # We need base_url = http://... without the model part
    # Model is the last slash-separated segment
    parts = rest.rsplit("/", 1)
    if len(parts) == 2:
        base_url, model = parts
    else:
        base_url, model = rest, "qwen3-coder:30b"

    if spec.startswith("openai-compat:"):
        backend = "openai-compat"
    else:
        backend = "ollama"

    return backend, base_url, model


def _call_llm(prompt: str, timeout: float) -> Optional[str]:
    """
    Call the configured watcher model and return raw text response.
    Returns None on any error or timeout.
    """
    import httpx  # type: ignore

    backend, base_url, model = _parse_model_spec(GOAL_WATCHER_MODEL)

    try:
        if backend == "ollama":
            url = f"{base_url}/api/generate"
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.2,
                    "num_predict": 512,
                },
            }
            with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=timeout, write=3.0, pool=3.0)) as client:
                r = client.post(url, json=payload)
                r.raise_for_status()
                return r.json().get("response", "")

        elif backend == "openai-compat":
            url = f"{base_url}/v1/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 512,
            }
            with httpx.Client(timeout=httpx.Timeout(connect=3.0, read=timeout, write=3.0, pool=3.0)) as client:
                r = client.post(url, json=payload)
                r.raise_for_status()
                choices = r.json().get("choices", [])
                if choices:
                    return choices[0].get("message", {}).get("content", "")

    except Exception as e:
        logger.warning(f"[goal_watcher] LLM call failed: {e!r}")

    return None


# ── Prompt ────────────────────────────────────────────────────────────────────

def _build_conversation_text(event: WatcherEvent) -> str:
    # The CURRENT user turn is the most important signal and is NOT part of
    # recent_turns (which the server fills from retrieved graph history —
    # empty for fresh sessions). Bug found 2026-06-09: omitting it meant the
    # model saw no conversation at all on new sessions and always answered
    # "no drift", so bootstrap adoption never fired.
    turns_text = f"[Turn 0 — CURRENT] User: {event.user_text[:400]}\n"
    for i, t in enumerate(event.recent_turns[:8]):
        turns_text += f"[Turn -{i+1}] User: {t.get('user','')[:300]}\n"
        turns_text += f"         Assistant: {t.get('assistant','')[:200]}\n"
    return turns_text


def _build_prompt_deanchored(event: WatcherEvent) -> str:
    """De-anchored two-stage prompt (2026-06-15 fix #1).

    Stage 1: state the current primary goal FROM THE CONVERSATION ALONE,
             ignoring any prior assumptions — this kills the anchoring where
             the model rationalizes keeping a stale stored goal.
    Stage 2: only THEN is the stored goal revealed, and the model classifies
             stage-1 vs stored as match | drifted | superseded.
    The NEW goal is always the stage-1 answer (current_goal).
    """
    turns_text = _build_conversation_text(event)
    stored = event.current_primary_goal or "(none on record)"

    return f"""You watch a conversation between {event.user} and an agent in pane "{event.pane_label}".

Recent conversation (newest first):
{turns_text}

Work through these stages IN ORDER. Be objective; do not assume continuity.

STAGE 1 — Ground truth from the conversation ALONE.
Ignoring any prior assumptions or records, read ONLY the conversation above and
state what the user is ACTUALLY trying to accomplish right now.
  current_goal: the user's current primary goal (1 sentence, ≤15 words)
  active_sub_goals: up to 4 sub-goals in progress (terse, ≤8 words each)
  completed: up to 3 items recently finished in this conversation (terse)

STAGE 2 — Compare to the record.
The goal we had ON RECORD was: "{stored}"
Compare your STAGE 1 current_goal to that record and classify:
  - "match"      : your current_goal is essentially the same work as the record.
  - "drifted"    : the work has shifted to a related but different goal.
  - "superseded" : the record is stale/abandoned; the conversation is about
                   something clearly different now.
If "drifted" or "superseded", the NEW primary goal is your STAGE 1 current_goal.

Respond ONLY as valid JSON. No prose, no markdown fences. Schema:
{{"current_goal": "...", "active_sub_goals": ["..."], "completed": ["..."], "comparison": "match|drifted|superseded", "evidence": "..."}}"""


def _build_prompt_anchored(event: WatcherEvent) -> str:
    """Legacy anchored prompt (pre-2026-06-15). Kept behind GOAL_WATCHER_DEANCHOR=0
    for rollback. KNOWN BUG: anchors the model on the stored goal, causing
    stale-goal self-perpetuation. Do not use as default."""
    turns_text = _build_conversation_text(event)
    return f"""You watch a conversation between {event.user} and an agent in pane "{event.pane_label}".

Last known primary goal: "{event.current_primary_goal or 'unknown'}"
Last active sub-goals: {json.dumps(event.current_active)}

Recent conversation (newest first):
{turns_text}

Tasks:
1. Has the primary goal changed significantly? (yes/no)
2. If yes: what is the new primary goal? (1 sentence, ≤15 words)
3. List currently active sub-goals (max 4, terse, ≤8 words each)
4. List recently completed items (max 3, only new since last check, terse)
5. Drift severity: none | minor | major
6. Brief evidence for "major" severity (1 sentence, or empty string)

Respond ONLY as valid JSON. No prose, no markdown fences. Example:
{{"goal_changed": false, "new_primary_goal": "", "active_sub_goals": ["..."], "completed": [], "drift_severity": "none", "evidence": ""}}"""


def _build_prompt(event: WatcherEvent) -> str:
    if GOAL_WATCHER_DEANCHOR:
        return _build_prompt_deanchored(event)
    return _build_prompt_anchored(event)


# ── Drift vote accumulator ────────────────────────────────────────────────────

_major_votes: dict[str, int] = {}  # session_id → consecutive major vote count
_turns_since_change: dict[str, int] = {}  # session_id → watcher passes since last genuine flip


# Canonical internal result shape consumed by _apply_drift_result:
#   current_goal: str        the model's anchor-free re-derived primary goal
#   active_sub_goals: list
#   completed: list
#   comparison: str          "match" | "drifted" | "superseded"
#   evidence: str
_CANON_DEFAULTS = {
    "current_goal": "",
    "active_sub_goals": [],
    "completed": [],
    "comparison": "match",
    "evidence": "",
}


def _normalize_result(data: dict) -> dict:
    """Map either prompt shape (de-anchored or legacy anchored) into the
    canonical internal result dict. Backward-safe defaults throughout."""
    out = dict(_CANON_DEFAULTS)

    if "comparison" in data or "current_goal" in data:
        # De-anchored shape.
        out["current_goal"] = (data.get("current_goal") or "").strip()
        comp = str(data.get("comparison", "match")).strip().lower()
        if comp not in ("match", "drifted", "superseded"):
            comp = "match"
        out["comparison"] = comp
    else:
        # Legacy anchored shape: goal_changed / new_primary_goal / drift_severity
        new_goal = (data.get("new_primary_goal") or "").strip()
        out["current_goal"] = new_goal
        severity = str(data.get("drift_severity", "none")).strip().lower()
        changed = bool(data.get("goal_changed"))
        if severity == "major" and changed:
            out["comparison"] = "superseded"
        elif changed or severity in ("major", "minor"):
            out["comparison"] = "drifted"
        else:
            out["comparison"] = "match"

    out["active_sub_goals"] = [
        a for a in (data.get("active_sub_goals") or []) if isinstance(a, str) and a.strip()
    ]
    out["completed"] = [
        c for c in (data.get("completed") or []) if isinstance(c, str) and c.strip()
    ]
    out["evidence"] = str(data.get("evidence", "") or "")
    return out


def _process_watcher_response(session_id: str, raw: str) -> dict:
    """Parse LLM JSON response into the canonical result shape; return safe
    defaults (comparison=match, no change) on parse failure."""
    try:
        # Strip any accidental markdown fences
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        data = json.loads(clean)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON is not an object")
        return _normalize_result(data)
    except Exception as e:
        logger.warning(f"[goal_watcher] JSON parse failed for session {session_id}: {e!r} raw={raw[:200]!r}")
        return dict(_CANON_DEFAULTS)


def _goal_tokens(s: str) -> set:
    """Lowercase token set for fuzzy goal comparison."""
    import re as _re
    return set(_re.findall(r"[a-z0-9]+", s.lower()))


def _goal_overlap_ratio(a: str, b: str) -> float:
    """Fraction of the smaller token set shared between two strings (0..1).
    Returns 0.0 if either side has no tokens."""
    ta, tb = _goal_tokens(a), _goal_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _goals_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """True if two goal strings share >= threshold of their smaller token set.

    Catches contradictions like active='Select assembly time approach' vs
    completed='Assembly time approach selected' that exact matching misses.
    """
    ratio = _goal_overlap_ratio(a, b)
    if ratio == 0.0:
        return False
    return ratio >= threshold


def _conversation_tokens_text(event: WatcherEvent) -> str:
    """Flatten the current turn + recent conversation into one string for
    token-overlap comparisons against the stored goal."""
    parts = [event.user_text or ""]
    for t in (event.recent_turns or [])[:8]:
        parts.append(t.get("user", "") or "")
        parts.append(t.get("assistant", "") or "")
    return " ".join(parts)


def _sanitize_contradictions(result: dict) -> bool:
    """Staleness/contradiction guard (2026-06-09).

    An item must not be both active and completed. A snapshot that lists
    'Select assembly time approach' as active while 'Assembly time approach
    selected' sits in completed renders as stale nonsense in the injected
    block. Completed wins; drop the contradicted active items in place.

    Returns True if any contradiction was found and removed.
    """
    completed = [c for c in (result.get("completed") or []) if isinstance(c, str) and c.strip()]
    actives = [a for a in (result.get("active_sub_goals") or []) if isinstance(a, str) and a.strip()]
    if not completed or not actives:
        return False
    kept = [a for a in actives if not any(_goals_overlap(a, c) for c in completed)]
    dropped = len(actives) - len(kept)
    if dropped:
        result["active_sub_goals"] = kept
        logger.info(f"[goal_watcher] contradiction guard dropped {dropped} active∩completed item(s)")
    return dropped > 0


def _is_stale(session_id: str, event: WatcherEvent) -> bool:
    """Fix #2 staleness clock: has the stored goal's last GENUINE change
    exceeded EITHER the turn or the time ceiling? Backward-compat: a missing
    changed_at (0.0) is treated as 'unknown' → maximally stale (eligible)."""
    if not GOAL_WATCHER_STALENESS:
        return False
    turns = _turns_since_change.get(session_id, 0)
    if turns >= GOAL_WATCHER_STALENESS_TURNS:
        return True
    changed_at = event.current_changed_at or 0.0
    # Unknown clock (legacy snapshot before changed_at existed) → stale.
    if changed_at <= 0.0:
        return True
    if (time.time() - changed_at) >= GOAL_WATCHER_STALENESS_SECONDS:
        return True
    return False


def _commit_goal_change(session_id: str, event: WatcherEvent, result: dict,
                        new_primary: str, confidence: float, reason: str) -> None:
    """Write a genuine goal flip and reset the per-session staleness counters."""
    from api.current_thing import update_snapshot_goals  # type: ignore
    actives = result.get("active_sub_goals") or event.current_active or []
    completed = result.get("completed") or []
    update_snapshot_goals(
        session_id=session_id,
        primary=new_primary,
        active=actives if isinstance(actives, list) else [],
        completed=completed if isinstance(completed, list) else [],
        source="llm",
        confidence=confidence,
        watcher_status="idle",
        change_reason=reason,
    )
    _major_votes[session_id] = 0
    _turns_since_change[session_id] = 0
    logger.info(
        f"[goal_watcher] goal FLIP ({reason}) session {session_id!r}: "
        f"-> {new_primary!r} evidence={result.get('evidence','')!r}"
    )
    print(f"[goal_watcher] FLIP ({reason}): {session_id[-30:]} -> {new_primary[:80]!r}", flush=True)


def _apply_drift_result(session_id: str, result: dict, event: WatcherEvent) -> None:
    """Apply a canonical (de-anchored) watcher result to the snapshot.

    Canonical result keys: current_goal, active_sub_goals, completed,
    comparison ('match'|'drifted'|'superseded'), evidence.
    """
    from api.current_thing import update_snapshot_goals, load_snapshot  # type: ignore

    comparison = result.get("comparison", "match")
    current_goal = (result.get("current_goal") or "").strip()

    # Count this watcher pass toward the staleness clock (reset on a real flip).
    _turns_since_change[session_id] = _turns_since_change.get(session_id, 0) + 1

    # Guard 1: strip active∩completed contradictions before anything else.
    had_contradiction = _sanitize_contradictions(result)

    # Cold-start bootstrap (bug found in Phase I testing 2026-06-09): when the
    # session has NO primary goal yet, adopt the model's anchor-free
    # re-derivation now as the initial goal (low confidence, unlocked — user
    # pin still wins, later drift can revise).
    if not event.current_primary_goal:
        inferred = current_goal
        actives = [a for a in (result.get("active_sub_goals") or []) if isinstance(a, str) and a.strip()]
        if not inferred and actives:
            inferred = actives[0]
        if inferred:
            update_snapshot_goals(
                session_id=session_id,
                primary=inferred,
                active=actives,
                completed=[c for c in (result.get("completed") or []) if isinstance(c, str)],
                source="llm",
                confidence=0.5,
                watcher_status="idle",
                change_reason="watcher-bootstrap",
            )
            _turns_since_change[session_id] = 0
            logger.info(f"[goal_watcher] bootstrap adopted goal for {session_id!r}: {inferred!r}")
            print(f"[goal_watcher] bootstrap: {session_id[-30:]} -> {inferred[:80]!r}", flush=True)
            return

    stored = event.current_primary_goal or ""
    conv_text = _conversation_tokens_text(event)
    # Overlap of stored goal vs the current conversation (fixes #2/#3 trigger).
    stored_vs_conv = _goal_overlap_ratio(stored, conv_text)
    # Overlap of the re-derived goal vs the stored goal (fix #2 divergence).
    rederived_vs_stored = _goal_overlap_ratio(current_goal, stored) if current_goal else 0.0

    # ── Fix #1: 'superseded' is an explicit signal the old anchor is dead.
    #            Flip immediately, bypassing the consecutive-vote threshold. ──
    if comparison == "superseded" and current_goal:
        _commit_goal_change(session_id, event, result, current_goal,
                            confidence=0.85, reason="watcher-superseded")
        return

    # ── Fix #3: a single 'drifted'/'major'-equivalent verdict with very low
    #            stored↔conversation overlap is a clear topic change — flip now.
    if comparison == "drifted" and current_goal and stored:
        if stored_vs_conv < GOAL_WATCHER_IMMEDIATE_FLIP_OVERLAP:
            _commit_goal_change(session_id, event, result, current_goal,
                                confidence=0.85, reason="watcher-immediate-flip")
            return

    # ── Fix #2: hard staleness ceiling. If the stored goal hasn't genuinely
    #            changed in too long AND the anchor-free re-derivation diverges
    #            from it, force a re-inference regardless of the vote. ──
    if (current_goal
            and rederived_vs_stored < GOAL_WATCHER_STALENESS_OVERLAP
            and _is_stale(session_id, event)):
        _commit_goal_change(session_id, event, result, current_goal,
                            confidence=0.7, reason="watcher-staleness-ceiling")
        return

    # ── Ambiguous drift: keep the consecutive-vote threshold (fix #3 retains
    #    MAJOR_DRIFT_THRESHOLD for the not-clearly-different case). 'drifted'
    #    counts as a major-ish vote; 'match' resets it. ──
    if comparison == "drifted":
        _major_votes[session_id] = _major_votes.get(session_id, 0) + 1
    else:
        _major_votes[session_id] = 0

    promote = _major_votes.get(session_id, 0) >= MAJOR_DRIFT_THRESHOLD

    if promote and current_goal:
        _commit_goal_change(session_id, event, result, current_goal,
                            confidence=0.85, reason="watcher-major-drift")
        return

    if comparison == "drifted" and current_goal:
        # Perceived drift but below the ratification threshold: record the
        # re-derived actives/completed at minor confidence WITHOUT flipping
        # primary (preserves the cautious 2-vote behavior for ambiguous drift).
        actives = result.get("active_sub_goals") or event.current_active or []
        completed = result.get("completed") or []
        update_snapshot_goals(
            session_id=session_id,
            active=actives if isinstance(actives, list) else [],
            completed=completed if isinstance(completed, list) else [],
            source="llm",
            confidence=0.6,
            watcher_status="idle",
            change_reason="watcher-minor-drift",
        )
        print(f"[goal_watcher] minor-drift (vote {_major_votes.get(session_id,0)}/"
              f"{MAJOR_DRIFT_THRESHOLD}): {session_id[-30:]}", flush=True)
        return

    # ── comparison == 'match' (no drift). Guard 2: staleness/divergence
    #    confidence decay (2026-06-09). If the re-derived actives share nothing
    #    with the stored actives, the stored goal is likely stale — decay
    #    confidence so the block renders tentative. (The hard staleness ceiling
    #    above already force-flips the genuinely-divergent stale cases.) ──
    model_actives = [a for a in (result.get("active_sub_goals") or [])
                     if isinstance(a, str) and a.strip()]
    stored_actives = [a for a in (event.current_active or []) if a and a.strip()]
    diverged = (
        bool(model_actives) and bool(stored_actives)
        and not any(_goals_overlap(m, s) for m in model_actives for s in stored_actives)
    )
    if diverged or had_contradiction:
        snap = load_snapshot(session_id)
        if snap is not None and not snap.goals.locked_by_user:
            old_conf = snap.goals.confidence
            new_conf = max(0.3, round(old_conf - 0.15, 2))
            if new_conf < old_conf:
                update_snapshot_goals(
                    session_id=session_id,
                    confidence=new_conf,
                    watcher_status="idle",
                    change_reason="watcher-staleness-decay",
                )
                logger.info(
                    f"[goal_watcher] staleness decay {session_id!r}: "
                    f"confidence {old_conf} -> {new_conf} "
                    f"(diverged={diverged} contradiction={had_contradiction})"
                )
    print(f"[goal_watcher] no-drift: {session_id[-30:]}", flush=True)


# ── Worker thread ──────────────────────────────────────────────────────────────

class GoalWatcher:
    """
    Singleton async watcher. Runs one background thread consuming a queue.
    Never blocks the FastAPI event loop.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[WatcherEvent] = queue.Queue(maxsize=100)
        self._last_call: dict[str, float] = {}  # session_id → last LLM call ts
        self._thread: Optional[threading.Thread] = None
        self._online = True

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="goal-watcher")
        self._thread.start()
        logger.info("[goal_watcher] Started background watcher thread")

    def enqueue(self, event: WatcherEvent) -> None:
        """
        Enqueue a new event for processing.
        Non-blocking — drops silently if queue is full (backpressure).
        """
        if not self._online:
            return

        # Skip trivially short turns
        if len(event.user_text.split()) < WATCHER_MIN_TOKENS:
            return

        # Guard: if recent_turns is empty the LLM has no grounding and will
        # hallucinate goals from thin air (observed 2026-06-11: FEA pane got
        # 'Resume agentic marketing pitch' because subchannel UUID mismatch
        # caused empty recency retrieval). Require at least 1 recent turn OR
        # a meaningful user_text before calling the LLM.
        if not event.recent_turns and len(event.user_text.split()) < WATCHER_MIN_TOKENS * 3:
            logger.debug(f"[goal_watcher] skipping: no recent_turns + short user_text for {event.session_id[-20:]!r}")
            return

        # Debounce: skip if we called LLM for this session recently
        last = self._last_call.get(event.session_id, 0.0)
        if time.time() - last < WATCHER_DEBOUNCE_S:
            return

        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.debug("[goal_watcher] Queue full — dropping event")

    def _run(self) -> None:
        """Background worker: drain queue, call LLM, apply results."""
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 5

        while True:
            try:
                event: WatcherEvent = self._queue.get(timeout=5.0)
            except queue.Empty:
                continue

            # Recheck debounce (may have sat in queue)
            last = self._last_call.get(event.session_id, 0.0)
            if time.time() - last < WATCHER_DEBOUNCE_S:
                continue

            self._last_call[event.session_id] = time.time()

            try:
                prompt = _build_prompt(event)
                raw = _call_llm(prompt, timeout=WATCHER_TIMEOUT_S)

                if raw is None:
                    consecutive_failures += 1
                    logger.warning(
                        f"[goal_watcher] LLM returned None "
                        f"(failure {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.error("[goal_watcher] Too many failures — marking offline")
                        self._online = False
                        # Update all active sessions with watcher_status=offline
                        # (best-effort; we don't track all session IDs)
                        try:
                            from api.current_thing import update_snapshot_goals  # type: ignore
                            update_snapshot_goals(
                                session_id=event.session_id,
                                watcher_status="offline",
                                change_reason="watcher-offline",
                            )
                        except Exception:
                            pass
                    continue

                consecutive_failures = 0
                result = _process_watcher_response(event.session_id, raw)
                _apply_drift_result(event.session_id, result, event)

            except Exception as e:
                logger.error(f"[goal_watcher] Unexpected error: {e!r}")


# ── Module-level singleton ────────────────────────────────────────────────────

_watcher: Optional[GoalWatcher] = None

def get_watcher() -> GoalWatcher:
    global _watcher
    if _watcher is None:
        _watcher = GoalWatcher()
    return _watcher

def start_watcher() -> None:
    """Start the background watcher thread. Call once at server startup."""
    get_watcher().start()

def notify_new_turn(
    session_id: str,
    pane_label: str,
    channel_label: Optional[str],
    user_text: str,
    recent_turns: list[dict],
    current_primary_goal: Optional[str],
    current_active: list[str],
    current_changed_at: float = 0.0,
) -> None:
    """
    Called from /ingest (or /assemble) to notify the watcher of a new turn.
    Non-blocking.
    """
    from api.current_thing import _heuristic_user_from_namespace  # type: ignore

    user = _heuristic_user_from_namespace(channel_label)
    event = WatcherEvent(
        session_id=session_id,
        pane_label=pane_label,
        user=user,
        user_text=user_text,
        recent_turns=recent_turns,
        current_primary_goal=current_primary_goal,
        current_active=current_active,
        current_changed_at=current_changed_at,
    )
    get_watcher().enqueue(event)
