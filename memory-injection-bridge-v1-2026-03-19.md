---
*Prepared by **Agent: Mei (梅)** — PhD candidate, Tsinghua KEG Lab. Memory systems specialist.*
*Running: anthropic/claude-sonnet-4-6*

*Coordinated by **Agent: Gaho** — OpenClaw primary assistant.*
*Running: anthropic/claude-sonnet-4-6*

*Human in the Loop: Garrett Kinsman*

---

# Memory Injection Bridge — v1-2026-03-19

## BLUF

ContextGraph is running and has an assembly API. OpenClaw injects MEMORY.md statically. The bootstrap hook that would wire them together doesn't exist yet — that's Rich's call. Until then: the 2 AM nightly cron now runs `update_memory_dynamic.py` after harvest+retag, which writes fresh ContextGraph context into MEMORY.md under static markers. Every session gets yesterday's best context baked in. It's a 24h lag kludge, but it works today.

Rich needs to add ~50 lines of TypeScript to make this real-time and query-aware.

---

## The Problem

MEMORY.md is injected at session start as a static file — same content every session, regardless of what the user is about to ask. ContextGraph is wired in as a plugin (`plugins.slots.contextEngine = contextgraph`) and has a working Python API (`assemble_for_session(query)`), but **that function is never called at session start**. The bootstrap path doesn't invoke it.

Result: every memory, decision, and project update indexed into ContextGraph from daily logs, session harvests, and file ingestion sits in the graph — but never reaches the agent. The agent wakes up with whatever was last manually written into MEMORY.md. ContextGraph runs nightly, indexes faithfully, and then does nothing for the session.

---

## What We Built (The Bridge)

**`scripts/update_memory_dynamic.py`** is a single-file bridge script that:

1. Calls `assemble_for_session()` from `context_injector.py` with a broad query (`"recent projects decisions infrastructure"`) and a 1500-token budget
2. Reads `MEMORY.md`
3. Finds or creates `<!-- DYNAMIC_CONTEXT_START -->` / `<!-- DYNAMIC_CONTEXT_END -->` markers
4. Replaces the section between markers with the fresh ContextGraph output + timestamp
5. Writes MEMORY.md back
6. Prints a one-line summary

If ContextGraph returns empty (graph not indexed, no matches), the script logs it and skips the write — no corrupted state.

This script is added as the final step in the nightly cron job (`4063a6a3-5a2b-4565-930c-5967560995db`) after harvest and retag complete.

---

## Current Cron Flow

```
2:00 AM
  └── memory_harvester.py        (index daily logs, project files → ContextGraph)
  └── [replay step]              (existing)
  └── Gemma retag                (existing)
  └── update_memory_dynamic.py   (NEW: assemble top context → MEMORY.md)

Next session start
  └── MEMORY.md injected (now includes fresh Dynamic Context section)
```

---

## Limitations of the Bridge

| Limitation | Impact |
|-----------|--------|
| **Query-blind** | Uses a fixed broad query, not the actual first user message — may retrieve irrelevant context |
| **24h lag** | Context is only as fresh as last night's harvest. Work done today isn't in tomorrow's context until the next cron run |
| **Fixed budget** | 1500 tokens, hardcoded. No session-type awareness (quick check vs. deep research) |
| **Not real-time** | Can't adapt to what the user actually needs in this session |
| **Static injection** | Still subject to bootstrap truncation limits if MEMORY.md gets too large |

The bridge makes ContextGraph useful. It doesn't make it good. Option A is the fix.

---

## What We Need From Rich

### Option A — Bootstrap Hook (Preferred, ~50 lines TypeScript)

Add a call in the session bootstrap path, **before system prompt assembly**:

```python
from projects.contextgraph_engine.scripts.context_injector import assemble_for_session

result = assemble_for_session(first_user_message)
if result["message_count"] > 0:
    system_prompt += "\n\n" + result["context_block"]
```

`context_injector.py` already handles the full pipeline: tag inference, similarity retrieval, token budgeting, markdown formatting. Rich just needs to call it.

**Benefits over the bridge:**
- Query-aware: retrieves what's actually relevant to this conversation
- Real-time: no lag, no cron dependency
- MEMORY.md can be slimmed down — dynamic context handles the heavy lifting

### Option B — Plugin Slot Callback

Extend the `contextEngine` plugin slot to support an `onSessionStart(query: string): string` callback. `context_injector.py` is already structured to serve this interface — `assemble_for_session()` takes a query string and returns a formatted markdown block. OpenClaw would call it and append the result to the system prompt.

This is cleaner architecture (plugin system handles it, no core change to bootstrap path) but requires extending the plugin slot API.

---

## Files

| File | Purpose |
|------|---------|
| `scripts/update_memory_dynamic.py` | Bridge script (kludge, works today) |
| `scripts/context_injector.py` | Assembly API (ready for Rich's hook) |
| `INTEGRATION.md` | Full original spec for Rich |

---

## Priority

**Medium-high.** The bridge is running and provides real value — fresh context in every session vs. stale static memory. Option A is the right fix: ~50 lines of TypeScript, high leverage, straightforward implementation. When Rich has bandwidth, Option A should be the target. Option B is the cleaner long-term architecture if the plugin slot gets extended anyway.

The `context_injector.py` API is stable. Rich doesn't need to touch Python — just call it.
