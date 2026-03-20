# MEMORY_INTEGRATION.md — How Context Graph Works With the Old Memory Paradigm

*For Garrett and other agents integrating Context Graph into an existing OpenClaw deployment.*

---

## Overview

Context Graph and the old memory paradigm (MEMORY.md + daily logs) are **complementary,
not competing**. They operate at different timescales and serve different purposes:

| Layer | What it is | Timescale | Managed by |
|-------|-----------|-----------|------------|
| **MEMORY.md** | Curated long-term facts, decisions, lessons | Weeks–months | Agent writes manually or via script |
| **Daily logs** (`memory/YYYY-MM-DD.md`) | Raw session notes, today's context | Days | Agent writes per-session |
| **Context Graph** | Tag-indexed message retrieval from recent sessions | Hours–weeks | Auto-indexed every turn |

The old paradigm handles *what the agent should always know*. Context Graph handles
*what's topically relevant right now*. They don't replace each other.

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

## Garrett's Approach: Disable Memory Graph, Run Context Graph With Old Paradigm

This is the recommended safe path for initial validation. Here's exactly what it means:

### What to disable

**Memory graph** = `scripts/update_memory_dynamic.py` — the nightly script that injects
assembled context directly into MEMORY.md (or SHADOWMEMORY.md). This is Phase 3.5 and
is still in shadow mode. **Leave this off** for now.

The script is only run manually or via cron — it's not part of the plugin or API. You
don't need to change any config to disable it; just don't schedule or run it.

### What to keep enabled

**Context Graph retrieval** = the plugin + API, which assembles and injects retrieved
context as a `[Retrieved Context]` block prepended to each incoming message.

The old memory files (MEMORY.md, daily logs) continue to load at session start exactly
as before. Context Graph adds retrieved session history on top of that — it doesn't
replace any of it.

### Result

Each turn the agent sees:
```
[System prompt: SOUL.md + IDENTITY.md + USER.md + MEMORY.md + daily log]
...
[Retrieved Context — from Context Graph /assemble]
Previous turn 1 (recent)
Previous turn 2 (recent)
Previous turn 3 (on-topic, 2 weeks ago)
...
[Current user message]
```

The MEMORY.md layer is unchanged. Context Graph adds a dynamic retrieval layer on top.

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

## Adding Dynamic Memory Injection Later (Phase 3.5)

Once Context Graph retrieval is stable, you can optionally layer in dynamic memory
injection — where a nightly script assembles the most relevant recent context and writes
it into a dedicated section of MEMORY.md.

This is Phase 3.5 and should be treated as a separate milestone:

```bash
# First, run in shadow mode for a few days
python3 scripts/update_memory_dynamic.py --shadow --dry-run  # preview only
python3 scripts/update_memory_dynamic.py --shadow            # writes to SHADOWMEMORY.md

# Review SHADOWMEMORY.md output manually — does it look right?
cat ~/.openclaw/workspace/SHADOWMEMORY.md

# If clean and representative for ~1-2 days, promote to live
python3 scripts/update_memory_dynamic.py --live              # writes to MEMORY.md
```

The script is safe by design:
- Skips write if `/assemble` returns empty
- Uses HTML comment markers (`<!-- DYNAMIC_CONTEXT_START/END -->`) to replace-in-place,
  never touching curated content above the section
- `--shadow` flag is the default; `--live` requires explicit opt-in

This is **not** required for Context Graph retrieval to work. It's an optional enhancement
that surfaces the most relevant graph content into the persistent memory layer.

---

## Summary

| What | State in Garrett's plan |
|------|------------------------|
| MEMORY.md + daily logs (old paradigm) | ✅ Keep, unchanged |
| Context Graph plugin + API (retrieval) | ✅ Enable, run in ghost mode |
| `update_memory_dynamic.py` (memory graph) | ❌ Disable — validate later as Phase 3.5 |
| Comparison log monitoring | ✅ Use to validate retrieval quality |
| Promote to production | After checklist above is green |

The goal: Context Graph adds a dynamic retrieval layer on top of the existing memory
stack without touching or replacing anything that already works. The old paradigm
handles long-term knowledge; the graph handles topical recency.

---

*See also: [`AGENT_SETUP.md`](AGENT_SETUP.md) for full operational detail, service
management, and diagnostics.*
