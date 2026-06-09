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
WATCHER_TIMEOUT_S = float(os.environ.get("GOAL_WATCHER_TIMEOUT_S", "5.0"))

# Minimum tokens in a user turn to bother running the watcher.
WATCHER_MIN_TOKENS = int(os.environ.get("GOAL_WATCHER_MIN_TOKENS", "50"))

# Max LLM calls per session per second (debounce).
WATCHER_DEBOUNCE_S = float(os.environ.get("GOAL_WATCHER_DEBOUNCE_S", "30.0"))

# Consecutive "major" drift votes required before triggering a major update.
MAJOR_DRIFT_THRESHOLD = int(os.environ.get("GOAL_WATCHER_MAJOR_THRESHOLD", "2"))


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

def _build_prompt(event: WatcherEvent) -> str:
    turns_text = ""
    for i, t in enumerate(event.recent_turns[:8]):
        turns_text += f"[Turn -{i}] User: {t.get('user','')[:300]}\n"
        turns_text += f"         Assistant: {t.get('assistant','')[:200]}\n"

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


# ── Drift vote accumulator ────────────────────────────────────────────────────

_major_votes: dict[str, int] = {}  # session_id → consecutive major vote count


def _process_watcher_response(session_id: str, raw: str) -> dict:
    """Parse LLM JSON response; return safe defaults on parse failure."""
    defaults = {
        "goal_changed": False,
        "new_primary_goal": "",
        "active_sub_goals": [],
        "completed": [],
        "drift_severity": "none",
        "evidence": "",
    }
    try:
        # Strip any accidental markdown fences
        clean = raw.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        data = json.loads(clean)
        for k in defaults:
            if k not in data:
                data[k] = defaults[k]
        return data
    except Exception as e:
        logger.warning(f"[goal_watcher] JSON parse failed for session {session_id}: {e!r} raw={raw[:200]!r}")
        return defaults


def _apply_drift_result(session_id: str, result: dict, event: WatcherEvent) -> None:
    """Apply parsed watcher result to the snapshot."""
    from api.current_thing import update_snapshot_goals  # type: ignore

    severity = result.get("drift_severity", "none")

    if severity == "major":
        _major_votes[session_id] = _major_votes.get(session_id, 0) + 1
    else:
        _major_votes[session_id] = 0

    # Only promote to major after N consecutive votes
    effective_severity = (
        "major"
        if _major_votes.get(session_id, 0) >= MAJOR_DRIFT_THRESHOLD
        else ("minor" if severity in ("major", "minor") else "none")
    )

    if effective_severity == "none":
        # Update watcher status only
        update_snapshot_goals(
            session_id=session_id,
            watcher_status="idle",
            change_reason="watcher-no-drift",
        )
        return

    new_primary = None
    if result.get("goal_changed") and result.get("new_primary_goal"):
        new_primary = result["new_primary_goal"]

    active = result.get("active_sub_goals") or event.current_active
    completed = result.get("completed") or []

    update_snapshot_goals(
        session_id=session_id,
        primary=new_primary,
        active=active if isinstance(active, list) else [],
        completed=completed if isinstance(completed, list) else [],
        source="llm",
        confidence=0.85 if effective_severity == "major" else 0.6,
        watcher_status="idle",
        change_reason=f"watcher-{effective_severity}-drift",
    )

    if effective_severity == "major":
        logger.info(
            f"[goal_watcher] MAJOR drift in session {session_id!r}: "
            f"goal={new_primary!r} evidence={result.get('evidence','')!r}"
        )
        _major_votes[session_id] = 0  # Reset after ratification


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
    )
    get_watcher().enqueue(event)
