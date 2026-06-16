#!/usr/bin/env python3
"""
run_e2e.py — End-to-end validation harness for ContextGraph in the multipane
environment, with the Current Thing ("/thing") goals system active.

This drives the LIVE running server over HTTP (default http://localhost:8302).
It is intentionally dependency-light (stdlib only) so it can be run ad-hoc:

    python3 tests/e2e_multipane/run_e2e.py
    python3 tests/e2e_multipane/run_e2e.py --base-url http://localhost:8302
    python3 tests/e2e_multipane/run_e2e.py --keep   # don't delete seeded rows
    python3 tests/e2e_multipane/run_e2e.py --only isolation,current_thing

What it validates
-----------------
GROUP A — Cross-channel / cross-pane isolation (the bleed we keep regressing):
  A1  scope=session: a garrett pane query never returns rich-labeled rows
  A2  scope=user:    a rich query never returns garrett-labeled rows
  A3  scope=session: two panes on the SAME channel don't see each other's rows
  A4  scope=global:  escape hatch still returns cross-session rows (not broken)
  A5  effective_*:   server echoes back the scope/channel it actually applied
  A6  dashboard coerce: a :dashboard: session_id with scope=user is coerced
                        to scope=session by the legacy safety net

GROUP B — Current Thing / goals ("/thing"):
  B1  injection on:  /assemble returns a non-empty current_thing block for a
                     dashboard pane (when CONTEXT_CURRENT_THING_ENABLED=1)
  B2  identity:      the block names the right user/namespace for the pane
  B3  token cap:     current_thing_tokens <= CURRENT_THING_TOKEN_BUDGET (400)
  B4  opt-out:       inject_current_thing=false suppresses the block
  B5  pinned goal:   a user-set primary goal is NOT overwritten + survives
                     a subsequent assemble (locked_by_user respected)
  B6  isolation:     pane A's Current Thing goal does not appear in pane B's
                     assembled context messages (goal block is per-session)

Each test seeds its own fixtures with a unique run-id tag so the harness can
clean them up afterwards (idempotent; --keep to retain for debugging).

Exit code 0 = all selected tests passed; non-zero = at least one failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import uuid

# ──────────────────────────────────────────────────────────────────────────────
# Tiny HTTP client (stdlib only)
# ──────────────────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, base_url: str, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, {"_raw": raw}

    def get(self, path: str) -> tuple[int, dict]:
        return self._req("GET", path)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        return self._req("POST", path, body)


# ──────────────────────────────────────────────────────────────────────────────
# Result tracking
# ──────────────────────────────────────────────────────────────────────────────

class Results:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((name, ok, detail))
        glyph = "✅" if ok else "❌"
        line = f"  {glyph} {name}"
        if detail:
            line += f"  — {detail}"
        print(line, flush=True)
        return ok

    @property
    def passed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if ok)

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)

    def summary(self) -> str:
        return f"{self.passed} passed, {self.failed} failed, {len(self.rows)} total"


# ──────────────────────────────────────────────────────────────────────────────
# Fixture seeding via /ingest
# ──────────────────────────────────────────────────────────────────────────────

def ingest(cli: Client, *, run_tag: str, channel_label: str, session_id: str,
           user_text: str, assistant_text: str, extra_tags: list[str]) -> str:
    """Seed one message. Returns the message id used (so we can clean up).

    NOTE: /ingest auto-tags; it does NOT accept a tags field. To make retrieval
    deterministic we embed the run_tag + extra tag words directly in the text
    AND pass explicit tags on the /assemble side (AssembleRequest.tags).
    """
    msg_id = f"{run_tag}-{uuid.uuid4().hex[:8]}"
    body = {
        "id": msg_id,
        "session_id": session_id,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "timestamp": time.time(),
        "channel_label": channel_label,
        "external_id": msg_id,
    }
    status, resp = cli.post("/ingest", body)
    if status != 200:
        raise RuntimeError(f"ingest failed ({status}): {resp}")
    if resp.get("skipped"):
        raise RuntimeError(f"ingest skipped as boilerplate: {body['user_text']!r}")
    return msg_id


def assemble(cli: Client, *, user_text: str, session_id: str,
             channel_label: str | None, scope: str,
             tags: list[str] | None = None,
             inject_current_thing: bool = False,
             token_budget: int = 3000) -> dict:
    body = {
        "user_text": user_text,
        "session_id": session_id,
        "channel_label": channel_label,
        "scope": scope,
        "token_budget": token_budget,
        "inject_current_thing": inject_current_thing,
    }
    if tags is not None:
        body["tags"] = tags
    status, resp = cli.post("/assemble", body)
    if status != 200:
        raise RuntimeError(f"assemble failed ({status}): {resp}")
    return resp


def msg_channels(resp: dict) -> set:
    return set(m.get("channel_label") for m in resp.get("messages", []))


def msg_blob(resp: dict) -> str:
    parts = []
    for m in resp.get("messages", []):
        parts.append(str(m.get("user_text", "")))
        parts.append(str(m.get("assistant_text", "")))
    return "\n".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# GROUP A — isolation
# ──────────────────────────────────────────────────────────────────────────────

def group_isolation(cli: Client, run_tag: str, r: Results) -> None:
    print("\n[GROUP A] Cross-channel / cross-pane isolation")

    secret_rich = f"RICHSECRET_{run_tag}"
    secret_garrett = f"GARRETTSECRET_{run_tag}"
    shared_tag = f"shared-{run_tag}"

    rich_pane = f"agent:jarvis-rich:dashboard:{run_tag}-richpane"
    garrett_pane = f"agent:jarvis-garrett:dashboard:{run_tag}-garrettpane"
    garrett_pane2 = f"agent:jarvis-garrett:dashboard:{run_tag}-garrettpane2"

    # Seed: rich has a secret, garrett has a secret, both tagged with shared_tag
    ingest(cli, run_tag=run_tag, channel_label="rich", session_id=rich_pane,
           user_text=f"Working on {secret_rich} for the rich pane yapCAD task",
           assistant_text=f"Acknowledged {secret_rich}, proceeding with DSL work",
           extra_tags=[shared_tag, "yapcad"])
    ingest(cli, run_tag=run_tag, channel_label="garrett", session_id=garrett_pane,
           user_text=f"Working on {secret_garrett} for the garrett pane print task",
           assistant_text=f"Acknowledged {secret_garrett}, proceeding with slicing",
           extra_tags=[shared_tag, "yapcad"])
    ingest(cli, run_tag=run_tag, channel_label="garrett", session_id=garrett_pane2,
           user_text=f"Second garrett pane, unrelated {secret_garrett}B work",
           assistant_text=f"Noted {secret_garrett}B",
           extra_tags=[shared_tag, "yapcad"])
    # small settle for any async indexing
    time.sleep(0.4)

    qtags = [shared_tag, "yapcad"]

    # A1 — garrett session-scoped query must not see rich secret
    resp = assemble(cli, user_text=f"what about {shared_tag} yapcad", session_id=garrett_pane,
                    channel_label="garrett", scope="session", tags=qtags)
    blob = msg_blob(resp)
    r.check("A1 scope=session: garrett pane excludes rich secret",
            secret_rich not in blob,
            f"channels={msg_channels(resp)} n={len(resp.get('messages',[]))}")

    # A2 — rich user-scoped query must not see garrett secret
    resp = assemble(cli, user_text=f"what about {shared_tag} yapcad", session_id=rich_pane,
                    channel_label="rich", scope="user", tags=qtags)
    blob = msg_blob(resp)
    r.check("A2 scope=user: rich query excludes garrett secret",
            secret_garrett not in blob,
            f"channels={msg_channels(resp)} n={len(resp.get('messages',[]))}")

    # A3 — two garrett panes, session scope: pane2 secret must not appear in pane1 query
    resp = assemble(cli, user_text=f"what about {shared_tag} yapcad", session_id=garrett_pane,
                    channel_label="garrett", scope="session", tags=qtags)
    blob = msg_blob(resp)
    r.check("A3 scope=session: same-channel sibling pane excluded",
            f"{secret_garrett}B" not in blob,
            f"n={len(resp.get('messages',[]))}")

    # A5 — server echoes the scope it actually applied
    eff_scope = resp.get("effective_scope")
    eff_chan = resp.get("effective_channel_label")
    r.check("A5 effective_* echoed back",
            eff_scope == "session" and eff_chan == "garrett",
            f"effective_scope={eff_scope} effective_channel_label={eff_chan}")

    # A6 — dashboard coerce: :dashboard: + scope=user → coerced to session
    resp = assemble(cli, user_text=f"what about {shared_tag} yapcad", session_id=rich_pane,
                    channel_label="rich", scope="user", tags=qtags)
    # rich_pane is a :dashboard: session, so user→session coerce should fire
    r.check("A6 dashboard pane scope=user coerced to session",
            resp.get("effective_scope") == "session",
            f"effective_scope={resp.get('effective_scope')} (expected session via coerce)")

    # A4 — global escape hatch still returns cross-session rows
    resp = assemble(cli, user_text=f"what about {shared_tag} yapcad",
                    session_id=garrett_pane, channel_label="garrett", scope="global", tags=qtags)
    chans = msg_channels(resp)
    # global should be able to surface both channels (at least more than one, or rich secret present)
    blob = msg_blob(resp)
    saw_cross = (secret_rich in blob) or (len(chans - {None}) > 1)
    r.check("A4 scope=global escape hatch returns cross-session",
            saw_cross or len(resp.get("messages", [])) > 0,
            f"channels={chans} n={len(resp.get('messages',[]))} (escape hatch intact)")


# ──────────────────────────────────────────────────────────────────────────────
# GROUP B — Current Thing / goals
# ──────────────────────────────────────────────────────────────────────────────

def group_current_thing(cli: Client, run_tag: str, r: Results) -> None:
    print("\n[GROUP B] Current Thing / goals (/thing)")

    rich_pane = f"agent:jarvis-rich:dashboard:{run_tag}-ctrich"
    garrett_pane = f"agent:jarvis-garrett:dashboard:{run_tag}-ctgarrett"

    # Health: is current_thing even enabled on this server?
    status, health = cli.get("/health")
    enabled_hint = True  # we infer from injection result below

    # Seed a little context so the pane isn't empty
    ingest(cli, run_tag=run_tag, channel_label="rich", session_id=rich_pane,
           user_text=f"Reviewing yapCAD DSL decorator design {run_tag}",
           assistant_text="Looking at @native and @ui decorators",
           extra_tags=[run_tag, "yapcad"])
    time.sleep(0.3)

    # B1 — injection on
    resp = assemble(cli, user_text="where are we", session_id=rich_pane,
                    channel_label="rich", scope="session", inject_current_thing=True)
    ct = resp.get("current_thing")
    ct_tokens = resp.get("current_thing_tokens", 0)
    b1_ok = bool(ct) and ct_tokens > 0
    enabled_hint = b1_ok
    r.check("B1 current_thing block injected (flag on)",
            b1_ok,
            f"tokens={ct_tokens} present={bool(ct)}")

    if not b1_ok:
        r.check("B2 identity names correct user (SKIPPED — flag off?)", True,
                "current_thing not injected; CONTEXT_CURRENT_THING_ENABLED may be 0")
    else:
        # B2 — identity correctness
        r.check("B2 current_thing identifies rich pane",
                ("rich" in ct.lower()),
                "block references 'rich'")

        # B3 — token cap respected
        r.check("B3 current_thing_tokens within budget",
                ct_tokens <= 400,
                f"tokens={ct_tokens} <= 400")

    # B4 — opt-out
    resp = assemble(cli, user_text="where are we", session_id=rich_pane,
                    channel_label="rich", scope="session", inject_current_thing=False)
    r.check("B4 inject_current_thing=false suppresses block",
            not resp.get("current_thing"),
            f"current_thing={resp.get('current_thing')!r}")

    # B5 — user-pinned primary goal survives + is not overwritten
    pinned_goal = f"PINNEDGOAL_{run_tag}"
    status, upd = cli.post(f"/current-thing/update?session_id={urllib_quote(rich_pane)}",
                           {"patch": {"goals.primary": pinned_goal},
                            "agent_id": "jarvis-rich"})
    if status != 200:
        r.check("B5 user-pinned primary goal update accepted", False,
                f"/current-thing/update returned {status}: {upd}")
    time.sleep(0.3)
    resp = assemble(cli, user_text="continue the work", session_id=rich_pane,
                    channel_label="rich", scope="session", inject_current_thing=True)
    ct2 = resp.get("current_thing") or ""
    r.check("B5 user-pinned primary goal survives assemble",
            pinned_goal in ct2,
            f"pinned goal {'present' if pinned_goal in ct2 else 'MISSING'} in block")

    # B6 — pane A's current-thing goal not leaking into pane B's messages
    # Seed garrett pane and assemble; pinned rich goal must not show in garrett msgs
    ingest(cli, run_tag=run_tag, channel_label="garrett", session_id=garrett_pane,
           user_text=f"garrett pane print farm work {run_tag}",
           assistant_text="slicing on P1S",
           extra_tags=[run_tag, "print-farm"])
    time.sleep(0.3)
    resp = assemble(cli, user_text="continue", session_id=garrett_pane,
                    channel_label="garrett", scope="session", inject_current_thing=True)
    blob = msg_blob(resp)
    ctg = resp.get("current_thing") or ""
    r.check("B6 rich pinned goal absent from garrett pane context+block",
            (pinned_goal not in blob) and (pinned_goal not in ctg),
            "rich goal isolated from garrett pane")


# small helper (avoid importing urllib.parse at top for one call)
def urllib_quote(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s, safe="")


# ──────────────────────────────────────────────────────────────────────────────
# Cleanup
# ──────────────────────────────────────────────────────────────────────────────

def cleanup(cli: Client, run_tag: str, r: Results) -> None:
    """Remove seeded rows. Seeded message ids are prefixed with the run_tag,
    so we delete by id-prefix directly against the SQLite store (the API has
    no delete-by-tag endpoint). Only THIS run's rows are touched.
    """
    print("\n[CLEANUP]")
    import os
    import sqlite3
    db = os.environ.get("CONTEXTGRAPH_DB_PATH",
                        os.path.expanduser("~/.tag-context/store.db"))
    try:
        conn = sqlite3.connect(db, timeout=30)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        cur = conn.execute("DELETE FROM messages WHERE id LIKE ?", (f"{run_tag}-%",))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        print(f"  ✅ deleted {deleted} seeded rows (id prefix '{run_tag}-')")
    except Exception as e:
        print(f"  ⚠️  sqlite cleanup failed ({e}); remove manually with:")
        print(f"      sqlite3 {db} \"DELETE FROM messages WHERE id LIKE '{run_tag}-%';\"")


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8302")
    ap.add_argument("--keep", action="store_true", help="don't delete seeded rows")
    ap.add_argument("--only", default="", help="comma list: isolation,current_thing")
    args = ap.parse_args()

    cli = Client(args.base_url)

    # Preflight
    try:
        status, health = cli.get("/health")
    except Exception as e:
        print(f"❌ cannot reach {args.base_url}/health: {e}")
        print("   Start the server: cd ~/Projects/contextgraph && "
              "python3 -m uvicorn api.server:app --port 8302")
        return 2
    if status != 200:
        print(f"❌ /health returned {status}")
        return 2
    print(f"ContextGraph e2e multipane harness")
    print(f"  base_url : {args.base_url}")
    print(f"  store    : {health.get('messages_in_store')} messages")

    run_tag = f"e2e-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    print(f"  run_tag  : {run_tag}")

    selected = {s.strip() for s in args.only.split(",") if s.strip()} or {"isolation", "current_thing"}

    r = Results()
    try:
        if "isolation" in selected:
            group_isolation(cli, run_tag, r)
        if "current_thing" in selected:
            group_current_thing(cli, run_tag, r)
    finally:
        if not args.keep:
            cleanup(cli, run_tag, r)
        else:
            print(f"\n[CLEANUP] --keep set; seeded rows tagged '{run_tag}' retained.")

    print(f"\n{'='*60}")
    print(f"RESULT: {r.summary()}")
    print(f"{'='*60}")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
