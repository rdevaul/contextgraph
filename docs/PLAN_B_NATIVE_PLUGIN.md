# Plan B: Native OpenClaw Context Engine Plugin

*Written: 2026-03-12*
*Status: Active — Plan of Record*

---

## Summary

Build contextgraph as a native **OpenClaw context engine plugin** that replaces
the linear sliding window with DAG-based context assembly at the framework level.
This provides a clean A/B comparison: `/graph on` switches the active context
engine from `"legacy"` (linear) to `"contextgraph"` (graph-based); `/graph off`
switches back. Default is `off` until validated.

---

## Why Native (vs. Shadow/Hybrid)

The shadow mode (Phase 2) proved the graph assembler delivers better context.
But hybrid injection (prepending graph context to a linear window) introduces
artifacts — duplicate context, ambiguous boundaries, and no clean A/B comparison.

A native context engine plugin replaces the context pipeline entirely:
- **Clean switch**: linear OR graph, not both
- **Real behavior**: tests actual system performance, not a simulation
- **No artifacts**: graph assembly is the context, not a preamble stapled on
- **Framework integration**: uses OpenClaw's plugin lifecycle (ingest, assemble,
  compact, afterTurn) rather than external scripts

---

## Architecture

### OpenClaw Context Engine Interface

OpenClaw's `ContextEngine` interface (`plugin-sdk/context-engine/types.d.ts`):

```
bootstrap(sessionId, sessionFile) → BootstrapResult
ingest(sessionId, message)        → IngestResult
assemble(sessionId, messages, tokenBudget) → AssembleResult
compact(sessionId, ...)           → CompactResult
afterTurn(sessionId, messages, ...) → void
dispose()                         → void
```

Our plugin implements each method:

| Method | contextgraph Implementation |
|---|---|
| `bootstrap` | Import existing session history into the graph store |
| `ingest` | Tag incoming message, add to graph (real-time, not nightly) |
| `assemble` | Run graph-based context assembly (recency + topic layers) |
| `compact` | Return `compacted: false` — the graph doesn't need lossy compaction |
| `afterTurn` | Tag the assistant response, log quality metrics |
| `dispose` | Close DB connections |

### Component Diagram

```
┌─────────────────────────────────────────────────────┐
│  OpenClaw Gateway                                   │
│                                                     │
│  ┌──────────────────┐     ┌──────────────────────┐  │
│  │ /graph command    │     │ Plugin Config        │  │
│  │ (toggle engine)   │────▶│ slots.contextEngine  │  │
│  └──────────────────┘     └──────────────────────┘  │
│                                    │                 │
│                    ┌───────────────┴──────────┐      │
│                    ▼                          ▼      │
│         ┌──────────────┐           ┌──────────────┐  │
│         │ Legacy Engine │           │ contextgraph │  │
│         │ (linear)      │           │ Engine       │  │
│         └──────────────┘           └──────┬───────┘  │
│                                           │          │
└───────────────────────────────────────────┼──────────┘
                                            │
                    ┌───────────────────────┐│
                    │ contextgraph API      ││ HTTP (localhost)
                    │ (Python FastAPI)      │◀┘
                    │                       │
                    │ ├── /tag    (infer tags)
                    │ ├── /ingest (store + tag)
                    │ ├── /assemble (graph retrieval)
                    │ └── /quality (metrics)
                    │                       │
                    │ ┌───────────────────┐ │
                    │ │ SQLite Store      │ │
                    │ │ (~/.tag-context/  │ │
                    │ │  store.db)        │ │
                    │ └───────────────────┘ │
                    └───────────────────────┘
```

### Two-Process Architecture

**TypeScript plugin** (runs in-process with OpenClaw Gateway):
- Thin wrapper implementing `ContextEngine` interface
- Calls Python API over localhost HTTP
- Registers `/graph` command
- Manages comparison logging

**Python API server** (separate process, launchd-managed):
- FastAPI server exposing the graph operations
- Houses all the existing Python code (tagger, assembler, store, quality agent)
- Runs GP evolution on a schedule (background task or cron)
- Port: 8300 (localhost only)

**Rationale:** The core graph logic is 1000+ lines of proven Python (GP evolution
via DEAP, spaCy NLP, SQLite store). Porting to TypeScript would be a rewrite
with no benefit. The HTTP bridge adds ~5ms latency per call — negligible compared
to LLM inference time.

---

## Implementation Plan

### Phase A: Python API Server

**Goal:** Expose the existing Python codebase as a FastAPI service.

**Endpoints:**

```
POST /tag
  Body: { user_text, assistant_text }
  Returns: { tags: [...], confidence: 0.85, per_tagger: {...} }

POST /ingest
  Body: { id, session_id, user_text, assistant_text, timestamp }
  Returns: { ingested: true, tags: [...] }

POST /assemble
  Body: { user_text, tags?, token_budget? }
  Returns: {
    messages: [{ id, user_text, assistant_text, tags, timestamp }],
    total_tokens: 3423,
    recency_count: 9,
    topic_count: 14,
    tags_used: [...]
  }

GET /health
  Returns: { status: "ok", messages_in_store: 816, tags: 16 }

GET /metrics
  Returns: { quality_scores: {...}, tagger_fitness: {...} }
```

**Files to create:**
- `api/server.py` — FastAPI app
- `api/requirements.txt` — fastapi, uvicorn
- Service plist for launchd

**Estimated effort:** ~2 hours

### Phase B: OpenClaw Plugin (TypeScript)

**Goal:** TypeScript plugin that bridges OpenClaw ↔ Python API.

**Plugin structure:**
```
~/.openclaw/extensions/contextgraph/
├── openclaw.plugin.json     # Plugin manifest
├── index.ts                  # Plugin entry point
├── engine.ts                 # ContextEngine implementation
├── api-client.ts             # HTTP client for Python API
├── logger.ts                 # Comparison logging
└── package.json
```

**Key implementation details:**

1. **`engine.ts` — ContextEngine implementation**

```typescript
class ContextGraphEngine implements ContextEngine {
    readonly info = {
        id: "contextgraph",
        name: "Context Graph",
        ownsCompaction: true,  // graph doesn't need lossy compaction
    };

    async ingest({ message }) {
        // Extract user/assistant text from AgentMessage
        // POST to Python /ingest endpoint
        // Return { ingested: true }
    }

    async assemble({ messages, tokenBudget }) {
        // Get last user message for tag inference
        // POST to Python /assemble endpoint
        // Convert response to AgentMessage[] format
        // Return { messages, estimatedTokens, systemPromptAddition? }
    }

    async compact() {
        // Graph doesn't compact — it grows and retrieves
        return { ok: true, compacted: false, reason: "graph-engine-no-compaction" };
    }

    async afterTurn({ messages }) {
        // Tag + store the assistant response
        // Log quality metrics
    }
}
```

2. **`/graph` command registration**

```typescript
api.registerCommand({
    name: "graph",
    description: "Toggle context graph engine (on/off)",
    acceptsArgs: true,
    handler: async (ctx) => {
        const mode = ctx.args?.trim().toLowerCase();
        if (mode === "on") {
            // Write state file, return instructions
            return { text: "🔀 Context graph engine activated. Using DAG-based assembly." };
        } else if (mode === "off") {
            return { text: "🔀 Switched back to linear context window." };
        }
        return { text: "Usage: /graph on | /graph off" };
    },
});
```

**Note on engine switching:** OpenClaw's `contextEngine` slot is set at config
level and resolved at gateway start. Runtime switching may require either:
- (a) Gateway restart after config change (simplest, but disruptive)
- (b) A runtime engine-swap mechanism (check if OpenClaw supports hot-swap)
- (c) A single engine that internally delegates to graph or legacy based on state

**Option (c) is most practical:** Register a "switchable" engine that reads a
state file and delegates to either graph assembly or pass-through (legacy
behavior). No gateway restart needed.

3. **Comparison logging**

When graph mode is active, log both assemblies:
```json
{
    "timestamp": "2026-03-12T11:00:00Z",
    "query_preview": "How do I fix the voice PWA...",
    "graph_assembly": {
        "messages": 24, "tokens": 3400,
        "recency": 9, "topic": 15,
        "tags": ["voice-pwa", "deployment"]
    },
    "linear_would_have": {
        "messages": 22, "tokens": 3700,
        "tags_present": ["voice-pwa", "shopping-list", "..."]
    }
}
```

Log file: `~/.tag-context/comparison-log.jsonl`

**Estimated effort:** ~3–4 hours

### Phase C: Integration & Testing

**Goal:** End-to-end testing with safe defaults.

1. Install plugin in `~/.openclaw/extensions/contextgraph/`
2. Start Python API server (launchd)
3. Configure OpenClaw:
   ```json
   {
       "plugins": {
           "entries": {
               "contextgraph": { "enabled": true }
           },
           "slots": {
               "contextEngine": "contextgraph"
           }
       }
   }
   ```
4. Gateway restart
5. Verify `/graph off` (default) uses legacy pass-through
6. `/graph on` — verify graph assembly is active
7. Test several real queries, review comparison logs
8. `/graph off` — verify clean switch back

**Test scenarios:**
- Cold start: Does bootstrap import session history?
- Topic switch: Query about voice-pwa after discussing shopping list
- Long-range recall: Reference a project discussed days ago
- Reframing: Deliberately re-establish context, verify graph handles it
- Fallback: `/graph off` restores normal behavior completely

**Estimated effort:** ~2 hours

### Phase D: Nightly Pipeline Migration

**Goal:** Move from external cron scripts to plugin-managed background tasks.

- GP evolution runs as a background task in the Python API server
- Harvester becomes unnecessary (ingest happens in real-time via the engine)
- Replay runs on demand or after evolution completes

**Estimated effort:** ~1 hour

---

## File Inventory

### New files (Python API)
```
api/
├── server.py              # FastAPI application
├── requirements.txt       # fastapi, uvicorn
└── test_api.py            # API endpoint tests
```

### New files (OpenClaw Plugin)
```
plugin/
├── openclaw.plugin.json   # Plugin manifest
├── index.ts               # Plugin entry (registers engine + command)
├── engine.ts              # ContextEngine implementation
├── api-client.ts          # HTTP client for Python API
├── logger.ts              # Comparison logger
└── package.json           # Plugin dependencies
```

### New files (Service)
```
service/
├── com.contextgraph.api.plist   # launchd plist for Python API
└── install.sh                    # Setup script
```

### Existing files (unchanged)
```
assembler.py, ensemble.py, features.py, gp_tagger.py,
logger.py, quality.py, reframing.py, store.py, tagger.py
```

---

## Risk Assessment

| Risk | Mitigation |
|---|---|
| Python API adds latency | Measured at ~5ms localhost; negligible vs. LLM |
| Engine switching breaks sessions | Switchable engine with state file; legacy is always fallback |
| Graph assembly returns poor context | `/graph off` instantly restores linear; comparison logs catch issues |
| Plugin SDK changes in OpenClaw updates | Pin OpenClaw version during testing; thin abstraction layer |
| GP tagger overfits on small corpus | Ensemble with baseline ensures minimum quality; fitness monitoring |

---

## Success Criteria

Same as Phase 2, but measured on **real interactions** (not shadow simulation):

1. **Reframing rate < 5%** when graph mode is active
2. **Context density > 55%** (relaxed from 60% given structural ceiling)
3. **No coherence regressions** — conversations feel at least as good as linear
4. **Token efficiency** — graph uses fewer tokens than linear for equivalent coverage
5. **Clean switching** — `/graph on` and `/graph off` work without restart or artifacts

---

## Timeline

| Phase | Effort | Dependency |
|---|---|---|
| A: Python API Server | ~2h | None |
| B: OpenClaw Plugin | ~3-4h | Phase A |
| C: Integration & Testing | ~2h | Phase A + B |
| D: Pipeline Migration | ~1h | Phase C validated |

**Total estimated effort: 8–9 hours of focused work.**

---

## Sticky Threads (P0 Enhancement)

See [`STICKY_THREADS.md`](STICKY_THREADS.md) for the full design. Graph mode
drops active tool call chains from context during multi-step operations, causing
the agent to lose track of in-progress work. The sticky thread layer pins active
work into context regardless of recency/topic scoring.

**Status:** Design complete, implementation in progress.

## Open Questions

1. **Hot-swapping context engines:** Does OpenClaw support changing the active
   context engine at runtime, or does it require a gateway restart? If restart
   required, the switchable-engine approach (option c) is the path.

2. **AgentMessage format:** The graph store uses plain text pairs. Need to map
   between `AgentMessage` (which includes tool calls, system messages, etc.) and
   the graph's simpler model. Tool-call messages may need special handling.

3. **Multi-session support:** The current graph is single-session. The plugin
   will receive `sessionId` for each call — should we maintain per-session graphs
   or a single shared graph? Shared is more powerful but needs access controls.

4. **systemPromptAddition:** The `AssembleResult` type includes an optional
   `systemPromptAddition` field. This could inject "retrieved context summary"
   or tag metadata into the system prompt. Worth experimenting with.
