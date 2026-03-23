# MEMORY_INTEGRATION.md — How Context Graph Works With Memory Paradigm

*For agents maintaining Context Graph alongside the existing MEMORY.md system.*

---

## Overview

Context Graph and the existing memory paradigm (MEMORY.md + daily logs) are **complementary,
not competing**. They operate at different timescales and serve different purposes:

| Layer | What it is | Timescale | Managed by |
|-------|-----------|-----------|------------|
| **MEMORY.md** | Curated long-term facts, decisions, lessons (+ Dynamic Context section) | Weeks–months | Agent + memory updater (every 4h) |
| **Daily logs** (`memory/YYYY-MM-DD.md`) | Raw session notes, today's context | Days | Agent writes per-session |
| **Context Graph** | Tag-indexed message retrieval from recent sessions | Hours–weeks | Auto-indexed every turn |

The memory paradigm handles *what the agent should always know*. Context Graph handles
*what's topically relevant right now*. The memory updater bridges them by injecting
graph-assembled summaries into MEMORY.md's Dynamic Context section.

---

## How the Old Memory Paradigm Works

At session start, the agent reads a stack of files:

1. `SOUL.md`, `IDENTITY.md` — who the agent is
2. `USER.md` — who it's helping
3. `MEMORY.md` — curated long-term memory (key facts, project state, lessons learned)
4. `memory/YYYY-MM-DD.md` (today + yesterday) — recent context

These are loaded into the system prompt / initial context. They're **static** for the
session — written once by a previous session's agent, read at startup.

**The problem this creates:** session context is large (2000–4000+ tokens) even when most
of it isn't relevant to the current conversation. And anything that happened more than
~2 days ago but isn't in MEMORY.md is invisible.

---

## How Context Graph Fills the Gap

When the plugin is active (`/graph on`), every incoming message triggers:

1. **`/assemble`** — query the graph for topically-relevant past turns
2. **Result** injected as `[Retrieved Context]` before the user message
3. **`/ingest`** — current turn stored and tagged for future retrieval

The assembler returns:
- **Recency layer** (25% of token budget) — most recent turns regardless of topic
- **Topic layer** (75% of budget) — turns matching inferred tags for this message

This means the agent has relevant context from *anywhere in the graph history*, not just
the last N messages or what made it into MEMORY.md. A conversation from three weeks ago
about a specific deployment decision surfaces when that deployment comes up again.

---

## Memory Integration: Live Status (v1.0-rc1)

As of v1.0-rc1, memory integration is **live and operational**. The `update_memory_dynamic.py`
script runs every 4 hours via launchd service (`com.glados.update-memory`), writing directly
to `MEMORY.md` with the `--live` flag.

### How it works

1. **Every 4 hours**, the launchd service triggers `scripts/update_memory_dynamic.py --live`
2. The script queries `/assemble` for topically-relevant context from the graph
3. Assembled content is summarized and formatted
4. The `## Dynamic Context` section in MEMORY.md is updated using HTML comment markers
   (`<!-- DYNAMIC_CONTEXT_START -->` ... `<!-- DYNAMIC_CONTEXT_END -->`)
5. Curated long-term memory sections above the markers are never touched

### What the agent sees

Each turn the agent sees:
```
[System prompt: SOUL.md + IDENTITY.md + USER.md + MEMORY.md (with Dynamic Context) + daily log]
...
[Retrieved Context — from Context Graph /assemble, live turn-by-turn]
Previous turn 1 (recent)
Previous turn 2 (recent)
Previous turn 3 (on-topic, 2 weeks ago)
...
[Current user message]
```

The agent gets **two layers of graph-assembled context**:
1. **Persistent layer** (MEMORY.md's Dynamic Context section) — updated every 4 hours, always loaded
2. **Live retrieval layer** (prepended to each message) — assembled per-turn based on inferred tags

This provides both stable persistent context (what's been relevant recently) and dynamic
turn-specific retrieval (what's relevant *right now*).

---

## Ghost Mode: Validating With API Logging

Before going to production, run the plugin in shadow/comparison mode and verify
retrieval quality through the API endpoints.

### Check retrieval quality (not just health)

```bash
# Service alive?
curl http://localhost:8300/health

# Retrieval actually working?
curl http://localhost:8300/quality | python3 -m json.tool
```

Key quality fields:
- `zero_return_rate` — should be <0.15 in normal use. >0.25 = problem.
- `tag_entropy` — should be >2.5. <2.0 = tags are too generic, topic layer degraded.
- `alert: true` — means one of the above thresholds is breached.

### Monitor the comparison log

Every turn, the plugin writes a record to `~/.tag-context/comparison-log.jsonl`:

```bash
# Stream live
tail -f ~/.tag-context/comparison-log.jsonl | python3 -m json.tool

# Or via API (last 50 turns)
curl "http://localhost:8300/comparison-log?limit=50" | python3 -m json.tool

# Quick summary — look for zero-return turns
curl http://localhost:8300/comparison-stats | python3 -m json.tool
```

The comparison log records `graph_assembly` vs `linear_assembly` for each turn:
```json
{
  "graph_assembly": {"messages": 4, "tokens": 1823, "recency": 2, "topic": 2},
  "linear_assembly": {"messages": 1, "tokens": 3651},
  "tags_used": ["deployment", "infrastructure"],
  "sticky_pins": 0
}
```

**What healthy looks like:**
- `graph_assembly.messages > 0` on most turns (topic layer is finding relevant content)
- `graph_assembly.tokens < linear_assembly.tokens` (graph is more efficient)
- `tags_used` contains specific tags, not just `["code", "openclaw"]` (discrimination working)

**What to watch for:**
- `graph_assembly.messages = 0` on many turns → check `/quality`, likely envelope pollution or tag issue
- `tags_used` is always `["code", "openclaw", "ai"]` → IDF filter may need tuning, corpus may be small

### Check what's been indexed

```bash
# Recent stored turns
python3 cli.py recent --n 20

# Tag distribution
python3 cli.py tags

# Or via API
curl http://localhost:8300/metrics | python3 -m json.tool
```

---

## Pushing to Production: Checklist

Once ghost mode looks clean, here's the validation gate before going production:

- [ ] `zero_return_rate < 0.15` over at least 50 turns
- [ ] `tag_entropy > 2.5` (tags are discriminating)
- [ ] Comparison log shows `graph.tokens < linear.tokens` on majority of turns
- [ ] No `alert: true` from `/quality` for 24+ hours
- [ ] `python3 -m pytest tests/ -v` — all 145 tests passing
- [ ] Manual spot-check: ask about something from 2+ weeks ago, verify it surfaces

When these are all green, the graph is working correctly alongside the old memory layer
and is ready for production use.

---

## Service Management

Memory integration runs via launchd service. To manage it:

```bash
# Check status
launchctl list | grep update-memory

# View logs (live)
tail -f /tmp/update_memory_dynamic.log

# Manually trigger an update (for testing)
/Users/rich/Projects/tag-context/venv/bin/python3 \
  /Users/rich/Projects/tag-context/scripts/update_memory_dynamic.py --live

# Unload service (to disable memory integration)
launchctl unload ~/Library/LaunchAgents/com.glados.update-memory.plist

# Reload service (after changes to plist or script)
launchctl unload ~/Library/LaunchAgents/com.glados.update-memory.plist
launchctl load ~/Library/LaunchAgents/com.glados.update-memory.plist
```

### Script flags

- `--live` — Write to `~/.openclaw/workspace/MEMORY.md` (production mode, currently active)
- `--shadow` — Write to `~/.openclaw/workspace/SHADOWMEMORY.md` (validation mode)
- `--dry-run` — Print output without writing any files (preview only)

The launchd service uses `--live` by default. For testing or validation, run manually
with `--shadow` or `--dry-run`.

### Safety features

The script is designed to be safe:
- Skips write if `/assemble` returns empty or API is unreachable
- Uses HTML comment markers to replace only the Dynamic Context section
- Never modifies curated long-term memory sections above the markers
- Logs all operations to `/tmp/update_memory_dynamic.log` with timestamps

---

## Summary (v1.0-rc1 Status)

| Component | Status |
|------|--------|
| MEMORY.md + daily logs (old paradigm) | ✅ Active, enhanced with Dynamic Context section |
| Context Graph plugin + API (retrieval) | ✅ Production, live turn-by-turn retrieval |
| `update_memory_dynamic.py` (memory integration) | ✅ Live via launchd, runs every 4 hours with `--live` |
| Dashboard (`/dashboard`) | ✅ Real-time quality and efficiency metrics |
| Automated turn filtering | ✅ Cron/heartbeat/subagent turns excluded from metrics |
| Lazy message summarization | ✅ Large turns summarized on-the-fly (Claude Haiku) |

Memory integration is **live and operational**. Context Graph adds:
1. **Dynamic retrieval layer** — turn-by-turn context assembly based on inferred tags
2. **Persistent dynamic context** — MEMORY.md section updated every 4 hours from graph
3. **Quality monitoring** — `/quality` endpoint + dashboard for health checks

The old memory paradigm remains unchanged and complementary. Graph handles topical
recency and discriminative retrieval; MEMORY.md continues to handle curated long-term
knowledge.

---

*See also: [`AGENT_SETUP.md`](AGENT_SETUP.md) for full operational detail, service
management, and diagnostics.*
