# contextgraph

Directed acyclic context graph for LLM context management — tag-based
retrieval replacing linear sliding windows.

**Status:** Dashboard at `/dashboard` provides real-time quality and efficiency metrics. Token efficiency: ~11.8% savings vs linear retrieval, 99%+ cache hit rate on context assembly.

## Problem

Standard LLM context management is temporal (flat sliding window). Compaction
blends unrelated topics into noise, and old-but-relevant context gets lost while
recent-but-irrelevant context takes up token budget. Users waste tokens
re-establishing context that should already be available.

## Approach

Every message/response pair is tagged with contextual labels. Context assembly
pulls from two layers:

1. **Recency layer** (25% of budget) — most recent messages regardless of tag
2. **Topic layer** (75% of budget) — messages retrieved by inferred tags for the
   incoming message, deduplicated against the recency layer

The underlying structure is a **DAG** (directed acyclic graph): time-ordered,
multi-tag membership, no cycles. The graph grows continuously and is never
discarded.

## Architecture

```
Incoming message
       │
       ▼
  FeatureExtractor ──► FixedTagger ──► inferred tags
                    (system + user tags)
                                              │
                                              ▼
                                    ContextAssembler
                                    ├── StickyLayer (pinned / tool-chain messages)
                                    ├── RecencyLayer (most recent N)
                                    └── TopicLayer  (by tag, deduped, IDF-filtered)
                                              │
                                              ▼
                                    Assembled context (oldest-first)
                                              │
                                              ├─────► Lazy summarization (large turns)
                                              │       └── Claude Haiku (configurable)
                                              ▼
                                    QualityAgent
                                    ├── Context density scoring
                                    ├── Reframing rate detection
                                    └── Filters cron/heartbeat/subagent turns
```

### Sticky/Pin Layer

The **sticky layer** ensures that explicitly pinned turns remain in context regardless of recency or topic score. This is useful for preserving critical context (requirements docs, architecture decisions, reference material) throughout a long conversation thread.

**How pins are created:**

1. **Manual pin** — Users send the `/pin` command in OpenClaw to pin a specific message.
2. **Automatic tool-chain detection** — The server checks `pending_chain_ids` on each `/assemble` call. When tool-use chains are active, it automatically creates/extends pins via `pin_manager.update_or_create_tool_chain_pin()`. This works even when the plugin loses track of pending chains (e.g. after a gateway restart), because the server falls back to inspecting recent messages.

**Data model:** Pins are stored in a separate `pins` table managed by the `pin_manager` module — there is no `is_sticky` column on the messages table.

**Config:** The `STICKY_BUDGET_FRACTION` environment variable controls how much of the token budget is reserved for pinned/sticky messages (default: `0.3` — 30% of total budget).

**Example use case:** In a "rocket design workflow" conversation, you might pin the initial requirements document turn so it persists through all subsequent back-and-forth, even as the conversation shifts through different subsystems and implementation details.

### User-Scoped Tags

Context Graph supports **user-specific tag definitions** alongside the global tag taxonomy. This allows individual users to customize tag ontology without affecting the shared baseline.

**Data files:**
```
data/system_tags.json                           # Global/system tag definitions
~/.tag-context/tags.user.registry/
  ├── <channel_label>.json                      # User-scoped tag registries
  ├── 994902066.json                            # (example: Rich's Telegram user ID)
  └── 510637988242522133.json                   # (example: Discord user ID)
```

**How it works:**
- System tags are loaded from `data/system_tags.json`
- User-scoped tags are stored as per-channel JSON files in `~/.tag-context/tags.user.registry/`
- User tags extend (not replace) the system tag set — both are used during tagging
- Each user/channel's tags are isolated to their own file to prevent conflicts

**API endpoints:**
```bash
# List all tag definitions for user (channel label)
curl http://localhost:8300/tags/user/glados-rich

# Add a new tag definition for a user
curl -X POST http://localhost:8300/tags/user/glados-rich/add \
  -H 'Content-Type: application/json' \
  -d '{"tag": "rocket-design", "keywords": ["propulsion", "nozzle", "thrust"]}'

# Delete a tag definition for a user
curl -X DELETE http://localhost:8300/tags/user/glados-rich/rocket-design
```

**Path traversal protection:** The API validates all user/channel label names to prevent directory traversal attacks (e.g., `../../../etc/passwd` is rejected).

**Use cases:**
- Personal assistant interactions with user-specific vocabulary (task management, reminders, scheduling, health tracking)
- Project-specific keywords for active work (e.g., `zheng-survey`, `skill-registry`)
- Domain experts with specialized terminology

### Channel Labels

Messages can be **channel-labeled** to scope context assembly per conversation or channel. This prevents context leakage between unrelated conversations when multiple channels share the same Context Graph instance.

**How it works:**
- Messages include an optional `channel_label` field during ingestion
- The OpenClaw plugin's `ContextEngine` includes `inferChannelLabel()` to automatically detect channel from conversation metadata
- Context assembly can be scoped to a specific channel by passing `channel_label` to the `/assemble` endpoint

**Example workflow:**
1. User sends message in Telegram channel `@rocket-team`
2. Plugin calls `inferChannelLabel()` → returns `"telegram:rocket-team"`
3. Message is ingested with `channel_label: "telegram:rocket-team"`
4. On context assembly for the next turn, plugin passes `channel_label: "telegram:rocket-team"`
5. Only messages from that channel are retrieved (recency + topic layers)

**API support:**
```bash
# Ingest with channel label
POST /ingest
Body: {
  "user_text": "...",
  "assistant_text": "...",
  "channel_label": "telegram:rocket-team"
}

# Assemble context scoped to channel
POST /assemble
Body: {
  "user_text": "...",
  "channel_label": "telegram:rocket-team"
}
```

**Benefits:**
- Prevents cross-channel context pollution in multi-tenant deployments
- Allows the same Context Graph instance to serve multiple isolated conversations
- Maintains per-channel topic continuity without manual namespace management

### Key features of the contextgraph system

- **Automated turn filtering** — Cron jobs, heartbeats, and subagent operations are automatically filtered from retrieval and quality metrics, preventing noise from diluting relevance scores.

- **Lazy message summarization** — When individual messages exceed 35% of the token budget, they're summarized on-the-fly using Claude Haiku (configurable model). This prevents giant turns from dominating the context window while preserving semantic content.

- **Degenerate text filtering** — Ingestion automatically rejects messages with excessive repetition (>40% duplicate 4-grams) or very short content (<10 characters), preventing noise from degrading tag quality and retrieval relevance.

- **IDF tag filtering** — Over-generic tags that apply to nearly all messages (e.g., "code", "openclaw") are automatically down-weighted using inverse document frequency, ensuring topic retrieval remains discriminative.

- **SQLite WAL mode** — Concurrent read/write access via write-ahead logging eliminates contention between API server, memory updater, and CLI tools.

- **99%+ cache hit rate** — Context assembly leverages prompt caching, achieving consistent cache hits across sequential turns.

## Performance Results (March 2026)

Production metrics across **580+ retrieval turns**, 4000-token budget:

### Graph vs. Linear — Head to Head

|                     | Context Graph | Linear Window |
|---------------------|---------------|---------------|
| Messages/query      | 23.6          | 22.0          |
| Tokens/query        | **3,423**     | 3,717         |
| Token efficiency    | **11.8% savings** | baseline    |
| Composition         | 9.0 recency + **14.6 topic** | 22.0 recency only |

### Key Metrics

| Metric                   | Value   | Target  | Status |
|--------------------------|---------|---------|--------|
| Topic retrieval rate     | 92.1%   | —       | ✅     |
| Context density          | 58.2%   | ~ 60%   | ✅     |
| Reframing rate           | 1.5%   | < 5%    | ✅     |
| Composite quality score  | 0.743   | —       | —      |
| Novel topic msgs/query   | 14.6    | —       | —      |
| Cache hit rate           | 99%+    | > 95%   | ✅     |

### Analysis

- **The graph delivers 14.6 topically-retrieved messages per query** that a
  linear window would never surface — older but on-topic exchanges that would
  have been compacted away or pushed out of the sliding window.

- **More relevant context in fewer tokens.** Graph assembly uses 294 fewer
  tokens per query while delivering more messages. This is because topic
  retrieval targets relevant material rather than blindly packing the most
  recent exchanges regardless of relevance.

- **Reframing rate of 1.5%** means users rarely need to re-establish context
  that was available in the graph. This is well under the 5% success target,
  which was estimated as typical for conventional linear context.

- **Context density at 58.2% is normal and expected.** This ceiling reflects
  structural overhead: the recency layer, topic layer, and sticky turns consume
  a predictable fraction of the budget. The remaining ~38% is the live retrieval
  window. The recency layer alone is fixed at 25% of token budget (~9 messages),
  so even perfect topic retrieval caps density around 62%. This is by design,
  not a deficiency. The density metric can be adjusted by tuning the recency/topic
  budget split if needed.

#### 1. Infinite budget mode (`--budget 999999`)

This tests **retrieval quality** — what the system retrieves, independent of budget pressure:

```bash
python3 scripts/shadow.py --report --budget 999999
```

With an artificially infinite budget, the **linear baseline expands to the entire history** (~583
messages in a mature corpus), while the **graph still selects ~22 targeted messages**.
This demonstrates what the graph actually does: semantic selection vs. a firehose.

This mode measures retrieval quality (precision, relevance) without budget constraints affecting the results.

#### 2. Production budget mode (default `--budget 4000`)

This tests the **full pipeline** — how budget pressure shapes results in a real deployment:

```bash
python3 scripts/shadow.py --report --budget 4000
```

This uses the actual production budget and tests the complete system, including how the recency/topic/sticky split behaves under real token constraints.

**Both modes are useful; they test different things.** The infinite budget mode isolates retrieval quality, while production budget mode validates the complete system behavior.

**Note:** The density metric becomes misleading without a budget cap. The 60% threshold
was calibrated for a 4k production budget where you want most assembled context to be
semantically relevant. With `--budget 999999`, the recency layer also expands and dilutes
the ratio — density will fail even when the graph is working correctly. The metrics that
remain meaningful at any budget:

| Metric | Still valid? |
|--------|-------------|
| Reframing rate | ✅ Always |
| Topic retrieval rate | ✅ Always |
| Novel msgs delivered | ✅ Always |
| Context density | ❌ Budget-dependent — ignore with large budgets |

## Components

| File | Purpose |
|---|---|
| `store.py` | SQLite MessageStore + tag index + `pin_manager` (sticky/pin layer) |
| `features.py` | Feature extraction (NLP + structural) |
| `tagger.py` | `FixedTagger` — loads tags from `data/system_tags.json` + user registries |
| `assembler.py` | Context assembly (sticky + recency + topic layers) |
| `quality.py` | Quality agent (density + reframing scoring) |
| `reframing.py` | Reframing signal detection |
| `logger.py` | Interaction logging |
| `cli.py` | CLI for manual testing |
| `scripts/shadow.py` | Phase 2 shadow mode evaluation |
| `utils/text.py` | Shared text utilities: `strip_envelope()` strips channel metadata before indexing |

## Operations

Context Graph runs as two launchd services on this machine:

### 1. API Server (`tag-context`)
- **Port:** 8300
- **Logs:** `/tmp/tag-context.log`
- **Dashboard:** http://localhost:8300/dashboard
- **Health check:** `curl http://localhost:8300/health`
- **Quality check:** `curl http://localhost:8300/quality`

The API server provides context assembly (`/assemble`), ingestion (`/ingest`), and quality monitoring endpoints for the OpenClaw plugin.

### Service management

```bash
# Check status
launchctl list | grep tag-context

# Restart API server (after code changes)
launchctl unload ~/Library/LaunchAgents/com.glados.tag-context.plist
launchctl load ~/Library/LaunchAgents/com.glados.tag-context.plist

# View logs
tail -f /tmp/tag-context.log
```

> **Note:** The `update-memory` launchd service and `update_memory_dynamic.py` script
> were removed 2026-03-31. Context is now delivered per-turn by the OpenClaw plugin's
> assembler — no static MEMORY.md injection needed.

### Dashboard

The Chart.js dashboard at http://localhost:8300/dashboard provides:
- **Scatterplot** — token efficiency visualization (graph vs linear)
- **Quality metrics** — density, reframing rate, cache hit rate
- **Token Savings** — percentage saved vs linear retrieval (green = saving, red = costing more)
- **Token overhead time series** — graph vs linear token usage over time
- **Tag distribution** — live tag hit counts from the registry (not cached historical counts)

All metrics support a **time window selector** (All Time / Last 7 Days / Last 24 Hours). Changing the window updates both the stats cards and the chart together — they always reflect the same dataset.

Tag frequency counts are sourced from the live `/tags` registry endpoint, not the comparison log, so they stay accurate without cache invalidation.

## Setup

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm   # optional but recommended
```

## Usage

```bash
# Add a message/response pair
python3 cli.py add "user text" "assistant text" [--tags extra_tag]

# Assemble context for an incoming message
python3 cli.py query "how do I fix the gateway?"

# Inspect the tag index
python3 cli.py tags

# View recent messages
python3 cli.py recent [--n 10]

# Run Phase 2 shadow evaluation
python3 scripts/shadow.py --report --verbose
```

## API Reference

The Context Graph API provides the following endpoints:

### Core Endpoints

#### POST `/ingest`
Ingest a user/assistant message pair into the graph.

**Request body:**
```json
{
  "user_text": "How do I fix the gateway?",
  "assistant_text": "To fix the gateway, you need to...",
  "channel_label": "telegram:rocket-team",  // optional
  "skip_automated_detection": false  // optional
}
```

**Response:**
```json
{
  "status": "ok",
  "message_id": 1234,
  "tags": ["networking", "infrastructure", "debugging"]
}
```

#### POST `/assemble`
Assemble context for an incoming message using the graph (recency + topic layers).

**Request body:**
```json
{
  "user_text": "What was the propulsion design decision?",
  "budget": 4000,  // optional, defaults to 4000
  "channel_label": "telegram:rocket-team"  // optional
}
```

**Response:**
```json
{
  "context": "...",  // assembled context (oldest-first)
  "messages_included": 23,
  "tokens_used": 3421,
  "tags_used": ["rocket-design", "propulsion"],
  "sticky_count": 2
}
```

#### GET `/health`
Health check endpoint. Returns service status and basic stats.

**Response:**
```json
{
  "status": "ok",
  "messages_in_store": 1024,
  "engine": "contextgraph"
}
```

#### GET `/quality`
Retrieval quality metrics. Returns zero-return rate, tag entropy, and alert status.

**Response:**
```json
{
  "turns_evaluated": 50,
  "zero_return_rate": 0.04,
  "tag_entropy": 3.65,
  "alert": false,
  "alert_reasons": [],
  "top_tags": ["code", "infrastructure", "networking"]
}
```

### User Tag Endpoints

#### GET `/tags/user/<name>`
List all tag definitions for a specific user.

**Example:**
```bash
curl http://localhost:8300/tags/user/alice
```

**Response:**
```json
{
  "user": "alice",
  "tags": {
    "rocket-design": {
      "keywords": ["propulsion", "nozzle", "thrust"],
      "patterns": ["rocket.*design", "engine.*test"]
    },
    "personal-tasks": {
      "keywords": ["todo", "reminder", "deadline"]
    }
  }
}
```

#### POST `/tags/user/<name>/add`
Add or update a tag definition for a user.

**Request body:**
```json
{
  "tag": "rocket-design",
  "keywords": ["propulsion", "nozzle", "thrust"],
  "patterns": ["rocket.*design"]  // optional
}
```

**Response:**
```json
{
  "status": "ok",
  "user": "alice",
  "tag": "rocket-design"
}
```

#### DELETE `/tags/user/<name>/<tag_name>`
Delete a tag definition for a user.

**Example:**
```bash
curl -X DELETE http://localhost:8300/tags/user/alice/rocket-design
```

**Response:**
```json
{
  "status": "ok",
  "user": "alice",
  "tag": "rocket-design",
  "deleted": true
}
```

### Other Endpoints

#### GET `/dashboard`
Web-based dashboard with Chart.js visualizations of token efficiency, quality metrics, and tag distribution.

#### GET `/comparison-log`
Returns the full comparison log (graph vs linear) as JSON.

#### POST `/compare`
Compare graph vs linear context for a given turn. Used by the plugin to generate comparison logs.

## Deployment (Python API as a Service)

The Python API (`api/server.py`) must be running for the OpenClaw plugin to function.
It's managed as a **launchd service** (`com.contextgraph.api`) so it survives reboots
and restarts automatically on crash.

### First-time setup

```bash
cd /path/to/tag-context
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Install the launchd service using the provided script (auto-detects your Python path):

```bash
./scripts/install-service.sh
```

The script reads `service/com.contextgraph.api.plist.template`, substitutes your local
paths, writes the rendered plist to `~/Library/LaunchAgents/`, and loads it.
The rendered plist is `.gitignore`'d so local paths never end up in the repo.

To use a specific Python interpreter (e.g. pyenv shim):

```bash
./scripts/install-service.sh --python ~/.pyenv/shims/python3
```

### Service management

```bash
# Status (PID present = running, just exit code = crashed)
launchctl list | grep tag-context

# Start / stop
launchctl start com.glados.tag-context
launchctl stop com.glados.tag-context

# Restart (e.g. after code changes — must unload+load to re-read plist)
launchctl unload ~/Library/LaunchAgents/com.glados.tag-context.plist
launchctl load ~/Library/LaunchAgents/com.glados.tag-context.plist

# Logs
tail -f /tmp/tag-context.log
```

### Health check

```bash
# Service up?
curl http://localhost:8300/health
# → {"status":"ok","messages_in_store":..., "engine":"contextgraph"}

# Retrieval actually working?
curl http://localhost:8300/quality
# → {"zero_return_rate":0.04,"tag_entropy":3.6,"alert":false,...}
```

> **Note:** `/health` tells you the service is running. `/quality` tells you whether
> retrieval is actually working. Always check both — a service can be healthy while
> silently returning empty context. See [Retrieval Quality Monitoring](#retrieval-quality-monitoring).

> **Note:** Never run the server manually (`python3 api/server.py` or `uvicorn ...`) while
> the launchd service is also active — port 8300 conflicts will cause both to crash-loop.
> Always use `launchctl stop` first, or `launchctl unload` to disable launchd management.

### OpenClaw plugin deployment

The plugin lives in `plugin/index.ts`. After making changes:

```bash
# Copy updated plugin to OpenClaw extension directory
cp plugin/index.ts ~/.openclaw/extensions/contextgraph/index.ts

# Graceful reload (keeps active sessions alive)
openclaw gateway reload
```

> ⚠️ **Do not use `openclaw gateway stop` or `gateway restart`** — these orphan the
> LaunchAgent and disconnect all active sessions (Telegram, Discord, Voice, etc.).
> Use `gateway reload` (SIGUSR1) instead. See [Notes for Agents](#notes-for-agents).

Toggle graph mode at runtime (in chat):
```
/graph on    # enable context graph
/graph off   # fall back to linear window
/graph       # show current status + API health
```

### Retrieval Quality Monitoring

The `/quality` endpoint provides retrieval health metrics that `/health` does not:

```bash
curl http://localhost:8300/quality | python3 -m json.tool
```

```json
{
  "turns_evaluated": 50,
  "zero_return_turns": 2,
  "zero_return_rate": 0.04,
  "avg_topic_messages": 3.2,
  "tag_entropy": 3.65,
  "corpus_size": 1024,
  "top_tags": [...],
  "alert": false,
  "alert_reasons": []
}
```

**Alert thresholds:**
- `zero_return_rate > 0.25` — more than 25% of recent turns returned no graph context
- `tag_entropy < 2.0` — tags are over-generic, topic layer is near-useless

When `alert: true`, check `alert_reasons` for which threshold was breached.

**Common causes of high zero_return_rate:**
1. **Envelope pollution** — channel metadata was being indexed as user text (fixed as of v1.1)
2. **Over-generic tags** — all messages tagged the same; IDF filtering mitigates this automatically
3. **Empty corpus** — not enough messages stored yet for topic retrieval to have anything to return

### Comparison logging

With graph mode on, after each turn the plugin calls `/compare` and appends a JSON
record to `~/.tag-context/comparison-log.jsonl` with:
- Graph vs. linear message/token counts
- Tags used for retrieval
- Sticky pin count (active tool chains)
- Whether the last turn had tool calls

```bash
tail -f ~/.tag-context/comparison-log.jsonl | python3 -m json.tool
# or via API:
curl http://localhost:8300/comparison-log
```

## Notes for Agents

### ⚠️ Gateway Restart

Do NOT use `openclaw gateway stop` / `gateway restart` to reload the plugin.
These commands disconnect all active sessions and orphan the LaunchAgent.

Use instead:
```bash
openclaw gateway reload   # SIGUSR1 graceful reload, keeps connections alive
```

### ⚠️ `/health` ≠ Retrieval Quality

`/health` returns `{"status":"ok"}` even when the graph is silently returning
empty context. Always check `/quality` when diagnosing retrieval problems:

```bash
curl http://localhost:8300/quality | python3 -c "import json,sys; q=json.load(sys.stdin); print('alert:', q['alert'], q.get('alert_reasons'))"
```

---

## Tests

```bash
python3 -m pytest tests/ -v
```

## Transition Roadmap

- [x] **Phase 1 — Passive Collection.** Harvest interactions, build the graph,
  evolve taggers. Corpus: 812+ interactions, 16 active tags.
- [x] **Phase 2 — Shadow Mode.** Validate graph assembly against linear baseline.
  Result: graph delivers more relevant context in fewer tokens (11.8% token savings).
- [x] **Phase 3 — Native Plugin.** OpenClaw context engine plugin live. `/graph on|off`
  toggles at runtime. Sticky threads auto-activate on tool chains. Comparison logging
  writes `~/.tag-context/comparison-log.jsonl` every turn. Dashboard at `/dashboard`
  provides real-time quality and efficiency metrics. See [`docs/PLAN_B_NATIVE_PLUGIN.md`](docs/PLAN_B_NATIVE_PLUGIN.md)
  for the full implementation plan.
- [x] **Phase 4 — Memory Integration (deprecated).**
  Superseded by per-turn plugin retrieval. Removed 2026-03-31.
- [ ] **Phase 5 — Graph-Primary.** After extended validation, graph becomes the default
  context engine. Linear window available as fallback via `/graph off`.

## Adapting for Your Domain

Context Graph is designed to be domain-agnostic and multi-agent capable. Here's how to adapt it for your specific use case:

### Custom Tags

Copy `tags.yaml` and edit the keywords/patterns for your domain. The tag configuration supports hot-reload, meaning you can update tag definitions without restarting the service:

```bash
# Edit your custom tag configuration
cp tags.yaml my-domain-tags.yaml
vim my-domain-tags.yaml

# The API server will detect changes and reload automatically
```

### Filtered Context Retrieval

You can retrieve context filtered by specific tags directly via the API:

```bash
# Only retrieve messages tagged with 'rocket-design'
curl -s 'http://localhost:8300/assemble?tags=rocket-design&budget=2000'

# Multiple tags (comma-separated)
curl -s 'http://localhost:8300/assemble?tags=rocket-design,propulsion&budget=2000'
```

This is useful for querying domain-specific context. _(The old `update_memory_dynamic.py`
script that used this for MEMORY.md injection was removed on 2026-03-31 — context is now
delivered per-turn by the plugin.)_

### Multi-Agent Deployments

Set the `AGENT_NAME` environment variable to namespace the SQLite database per agent. This allows multiple agents to run independently with separate context graphs:

```bash
# Agent 1
export AGENT_NAME=glados-rich
python3 api/server.py

# Agent 2 (different terminal/service)
export AGENT_NAME=glados-jarvis
python3 api/server.py
```

Each agent will maintain its own message store at `~/.tag-context/{AGENT_NAME}_messages.db`.

### Service Installation Per Agent

When using `install-service.sh`, pass `AGENT_NAME` as an environment variable to create separate launchd/systemd services per agent:

```bash
# Install service for agent 'glados-rich'
AGENT_NAME=glados-rich ./scripts/install-service.sh

# Install service for agent 'glados-jarvis' on a different port
AGENT_NAME=glados-jarvis PORT=8301 ./scripts/install-service.sh
```

This creates distinct service files (e.g., `com.glados.tag-context-glados-rich.plist`) and allows multiple agents to run concurrently on the same machine.

## Documentation

- [`docs/AGENT_SETUP.md`](docs/AGENT_SETUP.md) — **Operational guide for agents:**
  full setup, service management, diagnostics, and transition status.
  Start here if you're taking over maintenance.
- [`docs/CONTEXT_TRANSITION.md`](docs/CONTEXT_TRANSITION.md) — Design doc:
  the problem with linear context, the DAG vision, transition phases.
- [`docs/PLAN_B_NATIVE_PLUGIN.md`](docs/PLAN_B_NATIVE_PLUGIN.md) — Implementation
  plan for the native OpenClaw context engine plugin (Plan of Record).

## License

MIT
