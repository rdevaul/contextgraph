"""
Regression test for the 2026-06-17 Current Thing desync fix.

Bug: /assemble injected/updated Current Thing snapshots keyed on the RAW
request.session_id, while /current-thing GET/PATCH keyed on the resolver's
CANONICAL id. A pane arriving under an alias (UUID fragment / slug / label)
read+wrote a different snapshot row than the one a user's `/thing set`
(PATCH) wrote to -> cross-pane contamination and "my /thing set didn't take".

Fix: /assemble now resolves request.session_id via _resolve_session_id before
touching the snapshot, so both paths agree on the canonical key.

This is a LIVE test: it hits the running service on PORT (default 8302).
Skips cleanly if the service is down or the feature flag is off.
"""
import os
import uuid
import httpx
import pytest

BASE = f"http://localhost:{os.environ.get('CONTEXTGRAPH_PORT', '8302')}"


def _service_up() -> bool:
    try:
        r = httpx.get(f"{BASE}/health", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


def _ct_enabled() -> bool:
    # If the feature flag is off, /current-thing returns 503.
    try:
        r = httpx.get(f"{BASE}/current-thing", params={"session_id": "ping"}, timeout=2.0)
        return r.status_code != 503
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _service_up() or not _ct_enabled(),
    reason="contextgraph service not running or Current Thing flag off",
)


def test_assemble_and_patch_agree_on_canonical_key():
    """
    The divergence case that the fix targets: a pane reload sends /assemble
    under a SUFFIXED alias (`...-restored`) that the resolver strips back to
    the canonical id, while the user's `/thing set` (PATCH) ran under the
    UN-suffixed canonical.

    Pre-fix: /assemble keyed the snapshot on the raw suffixed id -> a DIFFERENT
    row than the PATCH wrote -> the patched goal is absent from the injected
    block. Post-fix: /assemble resolves first, both paths agree.
    """
    base_sid = f"webchat:rich:{uuid.uuid4()}"   # canonical session id
    alias_sid = f"{base_sid}-restored"           # suffixed form a reloaded pane sends
    goal = f"REGRESSION-{base_sid[-8:]} rebase router on wavefront core"

    # 1) Seed the canonical via /assemble so the resolver knows base_sid, then
    #    register the alias->canonical mapping by assembling under base_sid.
    seed = httpx.post(f"{BASE}/assemble", json={
        "session_id": base_sid,
        "channel_label": "mg-private:rich",
        "user_text": "seed",
        "inject_current_thing": True,
        "agent_id": "jarvis-rich",
    }, timeout=15.0)
    assert seed.status_code == 200, seed.text

    # 2) PATCH the primary goal under the CANONICAL (un-suffixed) id.
    patch = httpx.post(
        f"{BASE}/current-thing/update",
        params={"session_id": base_sid},
        json={"patch": {"goals.primary": goal}, "agent_id": "jarvis-rich"},
        timeout=10.0,
    )
    assert patch.status_code == 200, patch.text

    # 3) /assemble under the SUFFIXED alias. resolve() strips `-restored` ->
    #    base_sid, so the injected block must carry the patched goal.
    asm = httpx.post(f"{BASE}/assemble", json={
        "session_id": alias_sid,
        "channel_label": "mg-private:rich",
        "user_text": "continue",
        "inject_current_thing": True,
        "agent_id": "jarvis-rich",
    }, timeout=15.0)
    assert asm.status_code == 200, asm.text
    block = asm.json().get("current_thing") or ""
    assert goal in block, (
        "Patched goal missing from injected Current Thing block; "
        "assemble (suffixed alias) and patch (canonical) keyed different "
        "snapshot rows.\n"
        f"alias_sid={alias_sid}\nblock=\n{block}"
    )
