#!/usr/bin/env python3
"""
cgquery — ContextGraph forensic query & analysis tool
=====================================================

Inspect the context graph interactively from the command line. Browse by
user/channel, subchannel, session, and topic (tag); count matching records;
and dump the context window that WOULD be assembled for a given query — both
via direct DB query and via the live /assemble API endpoint, so the two can be
cross-checked.

The recency-floor layer is intentionally OMITTED from the assembled dump
(recency content is request-time-relative and "wouldn't exist" for a forensic
inspection, per the tool spec). The DB-direct assembly reproduces the
TOPIC (semantic tag) + STICKY/PIN layers, which are the layers that carry
cross-pane contamination risk.

USAGE
-----
  # Show the facets available (channels, subchannels, top tags, counts)
  cgquery facets

  # Count records matching a structured query
  cgquery count --channel garrett --subchannel fea
  cgquery count --tag yapCAD --channel rich
  cgquery count --session 'agent:jarvis-garrett:dashboard:2151...'

  # Browse matching rows (most recent first)
  cgquery browse --channel rich --tag debugging --limit 20
  cgquery browse --subchannel pid --show-text

  # Dump the assembled context window (topic + sticky), DB-direct
  cgquery assemble --query "where are we on the tank design" \
                   --channel garrett --subchannel fea --scope subchannel

  # Same, but hit the live API and DIFF the two result sets
  cgquery assemble --query "where are we on the tank design" \
                   --channel garrett --scope user --check-api

  # Pure API call (no DB reproduction)
  cgquery assemble --query "..." --channel rich --scope user --api-only

SCOPES (mirror assembler.assemble):
  global      no filtering
  user        filter topic+sticky by channel_label  (cross-USER isolation)
  session     filter topic+sticky by session_id      (cross-SESSION isolation)
  subchannel  filter recency by (channel,subchannel); topic falls to channel

Exit status is non-zero on --check-api mismatch, so this is CI-friendly.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import textwrap
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_DB = os.path.expanduser("~/.tag-context/store.db")
DEFAULT_API = "http://localhost:8302"
VALID_SCOPES = ("global", "user", "session", "subchannel")

# Reset to disable ANSI when piped
def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"

def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def red(s): return _c("31", s)
def green(s): return _c("32", s)
def yellow(s): return _c("33", s)
def cyan(s): return _c("36", s)


# ─────────────────────────────────────────────────────────────────────────────
# DB layer
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Row:
    id: str
    session_id: str
    user_id: str
    timestamp: float
    user_text: str
    assistant_text: str
    token_count: int
    channel_label: Optional[str]
    subchannel_label: Optional[str]
    is_automated: int
    tags: list = field(default_factory=list)


class DB:
    def __init__(self, path: str):
        if not os.path.exists(path):
            sys.exit(red(f"DB not found: {path}"))
        # read-only — forensic tool must never mutate the corpus
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row

    def _tags_for(self, ids: list[str]) -> dict[str, list[str]]:
        if not ids:
            return {}
        qmarks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT message_id, tag FROM tags WHERE message_id IN ({qmarks})", ids
        ).fetchall()
        out: dict[str, list[str]] = {i: [] for i in ids}
        for r in rows:
            out[r["message_id"]].append(r["tag"])
        return out

    def _to_rows(self, sqlrows) -> list[Row]:
        ids = [r["id"] for r in sqlrows]
        tmap = self._tags_for(ids)
        out = []
        for r in sqlrows:
            out.append(Row(
                id=r["id"], session_id=r["session_id"], user_id=r["user_id"],
                timestamp=r["timestamp"], user_text=r["user_text"],
                assistant_text=r["assistant_text"], token_count=r["token_count"],
                channel_label=r["channel_label"], subchannel_label=r["subchannel_label"],
                is_automated=r["is_automated"], tags=tmap.get(r["id"], []),
            ))
        return out

    # --- structured browse/count (the facet query) -------------------------
    def _where(self, channel, subchannel, session, tag, include_automated):
        clauses, params = [], []
        join = ""
        if tag:
            join = "INNER JOIN tags t ON m.id = t.message_id"
            clauses.append("t.tag = ?"); params.append(tag)
        if channel is not None:
            if channel == "<NULL>":
                clauses.append("m.channel_label IS NULL")
            else:
                clauses.append("m.channel_label = ?"); params.append(channel)
        if subchannel is not None:
            if subchannel == "<NULL>":
                clauses.append("m.subchannel_label IS NULL")
            else:
                clauses.append("m.subchannel_label = ?"); params.append(subchannel)
        if session is not None:
            clauses.append("m.session_id = ?"); params.append(session)
        if not include_automated:
            clauses.append("m.is_automated = 0")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return join, where, params

    def count(self, channel=None, subchannel=None, session=None, tag=None,
              include_automated=False) -> int:
        join, where, params = self._where(channel, subchannel, session, tag, include_automated)
        q = f"SELECT COUNT(DISTINCT m.id) AS n FROM messages m {join}{where}"
        return self.conn.execute(q, params).fetchone()["n"]

    def browse(self, channel=None, subchannel=None, session=None, tag=None,
               include_automated=False, limit=20) -> list[Row]:
        join, where, params = self._where(channel, subchannel, session, tag, include_automated)
        q = (f"SELECT DISTINCT m.* FROM messages m {join}{where} "
             f"ORDER BY m.timestamp DESC LIMIT ?")
        params = params + [limit]
        return self._to_rows(self.conn.execute(q, params).fetchall())

    # --- assembly reproduction (topic + sticky layers) ---------------------
    def get_by_tag_scoped(self, tag, limit, channel_label=None, session_id=None,
                          exclude_dashboard=False, include_automated=False) -> list[Row]:
        """Mirror of store.get_by_tag_scoped — the topic-layer retrieval."""
        clauses = ["t.tag = ?"]; params: list = [tag]
        if not include_automated:
            clauses.append("m.is_automated = 0")
        if channel_label is not None:
            clauses.append("m.channel_label = ?"); params.append(channel_label)
        if session_id is not None:
            clauses.append("m.session_id = ?"); params.append(session_id)
        if exclude_dashboard:
            clauses.append("(m.session_id IS NULL OR m.session_id NOT LIKE '%:dashboard:%')")
        params.append(limit)
        where = " AND ".join(clauses)
        q = (f"SELECT m.* FROM messages m INNER JOIN tags t ON m.id = t.message_id "
             f"WHERE {where} ORDER BY m.timestamp DESC LIMIT ?")
        return self._to_rows(self.conn.execute(q, params).fetchall())

    def facets(self):
        chans = self.conn.execute(
            "SELECT COALESCE(channel_label,'<NULL>') c, COUNT(*) n "
            "FROM messages GROUP BY channel_label ORDER BY n DESC").fetchall()
        subs = self.conn.execute(
            "SELECT COALESCE(subchannel_label,'<NULL>') s, COUNT(*) n "
            "FROM messages GROUP BY subchannel_label ORDER BY n DESC LIMIT 30").fetchall()
        tags = self.conn.execute(
            "SELECT tag, COUNT(*) n FROM tags GROUP BY tag ORDER BY n DESC LIMIT 30").fetchall()
        total = self.conn.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"]
        return chans, subs, tags, total


# ─────────────────────────────────────────────────────────────────────────────
# Tag inference (mirror of the API's path) — best-effort, falls back to API tags
# ─────────────────────────────────────────────────────────────────────────────
def infer_tags_via_api(api: str, text: str) -> list[str]:
    """Ask the running server to tag the query (so DB repro uses the SAME tags
    the API would). This keeps the two paths honest: identical tag set in."""
    try:
        body = json.dumps({"user_text": text, "assistant_text": ""}).encode()
        req = urllib.request.Request(f"{api}/tag", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read()).get("tags", [])
    except Exception as e:
        print(yellow(f"  (tag inference via API failed: {e}; pass --tags to override)"))
        return []


# ─────────────────────────────────────────────────────────────────────────────
# DB-direct assembly (topic + sticky/pins; NO recency floor)
# ─────────────────────────────────────────────────────────────────────────────
def assemble_db_direct(db: DB, query: str, tags: list[str], *, scope: str,
                       channel_label=None, session_id=None, subchannel_label=None,
                       per_tag_limit=20, token_budget=4000) -> dict:
    """Reproduce the TOPIC layer of assembler.assemble for the given scope.

    Scope semantics mirror assembler.assemble exactly:
      - global     : no channel/session filter on topic layer
      - user       : topic filtered by channel_label (cross-user isolation),
                     dashboard sessions excluded from channel recency (n/a here)
      - session    : topic filtered by session_id
      - subchannel : topic filtered by channel_label (subchannel narrows recency,
                     not the topic layer — matches assembler behavior)

    Sticky/pins are read live from the API (the pin manager is in-process), so we
    surface them separately and note that DB-direct can't see ephemeral pins.
    """
    chan_filter = None
    sess_filter = None
    exclude_dash = False
    if scope == "user":
        chan_filter = channel_label
        exclude_dash = True
    elif scope == "session":
        sess_filter = session_id
    elif scope == "subchannel":
        chan_filter = channel_label  # topic layer narrows to channel, per assembler
    # global: no filters

    seen: dict[str, Row] = {}
    per_tag: dict[str, int] = {}
    for tag in tags:
        rows = db.get_by_tag_scoped(
            tag, per_tag_limit, channel_label=chan_filter,
            session_id=sess_filter, exclude_dashboard=exclude_dash)
        per_tag[tag] = len(rows)
        for row in rows:
            seen[row.id] = row

    # Greedy budget fill, most-recent first (assembler fills by relevance then
    # recency; we approximate with recency, which is the audit-relevant ordering)
    msgs = sorted(seen.values(), key=lambda r: r.timestamp, reverse=True)
    chosen, tok = [], 0
    for m in msgs:
        est = m.token_count or max(1, (len(m.user_text) + len(m.assistant_text)) // 4)
        if tok + est > token_budget:
            continue
        chosen.append(m); tok += est
    chosen.sort(key=lambda r: r.timestamp)  # reading order
    return {"messages": chosen, "per_tag_counts": per_tag, "total_tokens": tok,
            "candidate_count": len(seen)}


# ─────────────────────────────────────────────────────────────────────────────
# API assembly
# ─────────────────────────────────────────────────────────────────────────────
def assemble_api(api: str, query: str, *, scope: str, channel_label=None,
                 session_id=None, subchannel_label=None, tags=None,
                 token_budget=4000) -> dict:
    payload = {
        "user_text": query, "scope": scope, "token_budget": token_budget,
        "recency_floor": 0,             # forensic: disable recency layer
        "inject_current_thing": False,  # forensic: no CT block
    }
    if tags: payload["tags"] = tags
    if channel_label is not None: payload["channel_label"] = channel_label
    if session_id is not None: payload["session_id"] = session_id
    if subchannel_label is not None: payload["subchannel_label"] = subchannel_label
    body = json.dumps(payload).encode()
    req = urllib.request.Request(f"{api}/assemble", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(red(f"API /assemble failed: {e.code} {e.read().decode()[:300]}"))
    except Exception as e:
        sys.exit(red(f"API /assemble error: {e}"))


# ─────────────────────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────────────────────
def fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def render_row(r: Row, show_text: bool):
    head = (f"{dim(fmt_ts(r.timestamp))}  "
            f"{cyan(r.channel_label or '<NULL>')}/{(r.subchannel_label or '<NULL>')}  "
            f"{dim(r.id[:12])}")
    print(head)
    print(f"    tags: {', '.join(r.tags) if r.tags else dim('(none)')}")
    if show_text:
        u = textwrap.shorten(r.user_text.replace('\n', ' '), 200)
        a = textwrap.shorten(r.assistant_text.replace('\n', ' '), 200)
        print(f"    {bold('U:')} {u}")
        print(f"    {bold('A:')} {a}")

def render_assembly(title: str, msgs: list, get):
    print(bold(f"\n=== {title}  ({len(msgs)} msgs) ==="))
    for m in msgs:
        ts = get(m, "timestamp"); cl = get(m, "channel_label"); sid = get(m, "session_id")
        sub = get(m, "subchannel_label", None)
        mid = get(m, "id")
        print(f"{dim(fmt_ts(ts))}  {cyan(cl or '<NULL>')}"
              + (f"/{sub}" if sub else "") + f"  {dim((sid or '')[:40])}  {dim(mid[:10])}")
        u = textwrap.shorten((get(m, "user_text") or "").replace('\n', ' '), 160)
        print(f"    {u}")


# ─────────────────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────────────────
def cmd_facets(db: DB, args):
    chans, subs, tags, total = db.facets()
    print(bold(f"\nTotal records: {total}\n"))
    print(bold("Channels (user):"))
    for r in chans: print(f"  {r['c']:<24} {r['n']:>6}")
    print(bold("\nSubchannels (top 30):"))
    for r in subs: print(f"  {r['s']:<32} {r['n']:>6}")
    print(bold("\nTags (top 30):"))
    for r in tags: print(f"  {r['tag']:<24} {r['n']:>6}")

def cmd_count(db: DB, args):
    n = db.count(channel=args.channel, subchannel=args.subchannel,
                 session=args.session, tag=args.tag,
                 include_automated=args.include_automated)
    facets = []
    if args.channel: facets.append(f"channel={args.channel}")
    if args.subchannel: facets.append(f"subchannel={args.subchannel}")
    if args.session: facets.append(f"session={args.session[:24]}…")
    if args.tag: facets.append(f"tag={args.tag}")
    print(f"{bold(str(n))} records match  [{', '.join(facets) or 'all'}]")

def cmd_browse(db: DB, args):
    rows = db.browse(channel=args.channel, subchannel=args.subchannel,
                     session=args.session, tag=args.tag,
                     include_automated=args.include_automated, limit=args.limit)
    print(bold(f"\n{len(rows)} rows (most recent first):\n"))
    for r in rows:
        render_row(r, args.show_text)

def cmd_assemble(db: DB, args):
    scope = args.scope
    if scope not in VALID_SCOPES:
        sys.exit(red(f"invalid scope {scope!r}; expected {VALID_SCOPES}"))

    # Resolve tag set: explicit --tags wins, else infer via API (so both paths
    # use the SAME tags the server would).
    tags = args.tags.split(",") if args.tags else infer_tags_via_api(args.api, args.query)
    print(dim(f"query tags: {tags or '(none)'}    scope={scope}"))

    api_res = None
    if not args.db_only:
        api_res = assemble_api(
            args.api, args.query, scope=scope, channel_label=args.channel,
            session_id=args.session, subchannel_label=args.subchannel,
            tags=tags, token_budget=args.token_budget)
        msgs = api_res.get("messages", [])
        # forensic: drop any recency_floor-sourced rows defensively
        msgs = [m for m in msgs if m.get("source") != "recency_floor"]
        render_assembly(f"API /assemble (scope={scope})", msgs,
                        lambda m, k, d=None: m.get(k, d))

    db_res = None
    if not args.api_only:
        db_res = assemble_db_direct(
            db, args.query, tags, scope=scope, channel_label=args.channel,
            session_id=args.session, subchannel_label=args.subchannel,
            per_tag_limit=args.per_tag_limit, token_budget=args.token_budget)
        render_assembly(f"DB-direct topic layer (scope={scope})", db_res["messages"],
                        lambda m, k, d=None: getattr(m, k, d))
        print(dim(f"  candidates={db_res['candidate_count']} "
                  f"per-tag={db_res['per_tag_counts']} tokens≈{db_res['total_tokens']}"))

    # cross-check
    if args.check_api and api_res is not None and db_res is not None:
        api_ids = {m["id"] for m in api_res.get("messages", [])
                   if m.get("source") != "recency_floor"}
        db_ids = {m.id for m in db_res["messages"]}
        only_api = api_ids - db_ids
        only_db = db_ids - api_ids
        print(bold("\n=== CROSS-CHECK (API vs DB-direct) ==="))
        print(f"  API rows:       {len(api_ids)}")
        print(f"  DB-direct rows: {len(db_ids)}")
        print(f"  intersection:   {len(api_ids & db_ids)}")
        if only_api: print(yellow(f"  only in API ({len(only_api)}): {[i[:10] for i in list(only_api)[:8]]}"))
        if only_db:  print(yellow(f"  only in DB  ({len(only_db)}): {[i[:10] for i in list(only_db)[:8]]}"))

        # CONTAMINATION CHECK: if a channel filter was requested, flag any API
        # row whose channel_label doesn't match — that's a leak.
        leak = []
        if args.channel and scope in ("user", "subchannel"):
            for m in api_res.get("messages", []):
                if m.get("source") == "recency_floor":
                    continue
                cl = m.get("channel_label")
                if cl is not None and cl != args.channel:
                    leak.append((m["id"][:10], cl))
        if leak:
            print(red(f"\n  ⚠ CROSS-CHANNEL LEAK: {len(leak)} API rows from other channels "
                      f"despite channel={args.channel} scope={scope}:"))
            for mid, cl in leak[:12]:
                print(red(f"      {mid}  channel={cl}"))
            sys.exit(2)
        elif args.channel and scope in ("user", "subchannel"):
            print(green(f"  ✓ no cross-channel leak (all API rows match channel={args.channel} or NULL)"))

        if only_api or only_db:
            print(yellow("\n  NOTE: divergence is expected — DB-direct reproduces only the topic"
                         "\n  layer; API also includes sticky/pins and relevance-ranked fill."))


# ─────────────────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(
        prog="cgquery", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default {DEFAULT_DB})")
    p.add_argument("--api", default=DEFAULT_API, help=f"API base (default {DEFAULT_API})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("facets", help="list channels/subchannels/tags with counts")

    def add_filters(sp):
        sp.add_argument("--channel", help="channel_label (user). Use <NULL> for unlabeled")
        sp.add_argument("--subchannel", help="subchannel_label. Use <NULL> for unlabeled")
        sp.add_argument("--session", help="exact session_id")
        sp.add_argument("--tag", help="topic tag")
        sp.add_argument("--include-automated", action="store_true")

    c = sub.add_parser("count", help="count matching records")
    add_filters(c)

    b = sub.add_parser("browse", help="list matching rows (recent first)")
    add_filters(b)
    b.add_argument("--limit", type=int, default=20)
    b.add_argument("--show-text", action="store_true", help="include user/assistant text")

    a = sub.add_parser("assemble", help="dump assembled context window (topic+sticky, no recency)")
    a.add_argument("--query", required=True, help="the incoming query text")
    a.add_argument("--channel", help="channel_label for scoping")
    a.add_argument("--subchannel", help="subchannel_label for scoping")
    a.add_argument("--session", help="session_id for scoping")
    a.add_argument("--scope", default="user", help=f"one of {VALID_SCOPES}")
    a.add_argument("--tags", help="comma-separated tag override (skip API inference)")
    a.add_argument("--token-budget", type=int, default=4000)
    a.add_argument("--per-tag-limit", type=int, default=20)
    a.add_argument("--check-api", action="store_true",
                   help="diff DB-direct vs API and flag cross-channel leaks (exit 2 on leak)")
    a.add_argument("--api-only", action="store_true", help="only call the API")
    a.add_argument("--db-only", action="store_true", help="only do DB-direct reproduction")
    return p


def main():
    args = build_parser().parse_args()
    db = DB(args.db)
    {"facets": cmd_facets, "count": cmd_count,
     "browse": cmd_browse, "assemble": cmd_assemble}[args.cmd](db, args)


if __name__ == "__main__":
    main()
