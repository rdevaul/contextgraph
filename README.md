# contextgraph

Directed acyclic context graph for LLM context management — tag-based
retrieval replacing linear sliding windows.

**Status:** Phase 2 (Shadow Mode) complete — graph assembly validated against linear baseline.

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
  FeatureExtractor ──► EnsembleTagger ──► inferred tags
                        ├── v0 baseline       │
                        └── GP-evolved        │
                                              ▼
                                    ContextAssembler
                                    ├── RecencyLayer (most recent N)
                                    └── TopicLayer  (by tag, deduped)
                                              │
                                              ▼
                                    Assembled context (oldest-first)
                                              │
                                              ▼
                                    QualityAgent
                                    ├── Context density scoring
                                    └── Reframing rate detection
```

## Phase 2 Performance Results (March 2026)

Shadow mode evaluation across **812 interactions**, 4000-token budget:

### Graph vs. Linear — Head to Head

|                     | Context Graph | Linear Window |
|---------------------|---------------|---------------|
| Messages/query      | 23.6          | 22.0          |
| Tokens/query        | **3,423**     | 3,717         |
| Composition         | 9.0 recency + **14.6 topic** | 22.0 recency only |

### Key Metrics

| Metric                   | Value   | Target  | Status |
|--------------------------|---------|---------|--------|
| Topic retrieval rate     | 92.1%   | —       | —      |
| Context density          | 58.2%   | > 60%   | ❌ (see note) |
| Reframing rate           | 1.5%   | < 5%    | ✅     |
| Composite quality score  | 0.743   | —       | —      |
| Novel topic msgs/query   | 14.6    | —       | —      |
| Token efficiency         | -294/query vs. linear | — | ✅ |

### Analysis

- **The graph delivers 14.6 topically-retrieved messages per query** that a
  linear window would never surface — older but on-topic exchanges that would
  have been compacted away or pushed out of the sliding window.

- **More relevant context in fewer tokens.** Graph assembly uses 294 fewer
  tokens per query while delivering more messages. This is because topic
  retrieval targets relevant material rather than blindly packing the most
  recent exchanges regardless of relevance.

- **Reframing rate of 1.5%** means users rarely need to re-establish context
  that was available in the graph. This is well under the 5% success target.

- **Density at 58.2%** is just under the 60% target. This is a structural
  artifact: the recency layer is fixed at 25% of token budget (~9 messages),
  so even perfect topic retrieval caps density around 62%. Adjustable by
  tuning the recency/topic budget split.

### GP Tagger Fitness (20 tags)

Top-performing tags (fitness ≥ 0.90):
`code`, `infrastructure`, `networking`, `question`, `shopping-list`, `llm`,
`openclaw`, `voice-pwa`, `research`, `ai`, `deployment`, `devops`, `security`

Mid-range (0.70–0.90): `planning`, `context-management`, `rl`

Low-data tags (0.495): `api`, `debugging`, `personal`, `yapCAD`

## Components

| File | Purpose |
|---|---|
| `store.py` | SQLite MessageStore + tag index |
| `features.py` | Feature extraction (NLP + structural) |
| `tagger.py` | Rule-based baseline tagger (v0) |
| `gp_tagger.py` | Genetically-evolved tagger (DEAP) |
| `ensemble.py` | Weighted mixture model over tagger family |
| `assembler.py` | Context assembly (recency + topic layers) |
| `quality.py` | Quality agent (density + reframing scoring) |
| `reframing.py` | Reframing signal detection |
| `logger.py` | Interaction logging |
| `cli.py` | CLI for manual testing |
| `scripts/harvester.py` | Nightly interaction collection |
| `scripts/evolve.py` | GP tagger retraining |
| `scripts/replay.py` | Ensemble retagging of full corpus |
| `scripts/shadow.py` | Phase 2 shadow mode evaluation |

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
curl http://localhost:8300/health
# → {"status":"ok","messages_in_store":..., "engine":"contextgraph"}
```

> **Note:** Never run the server manually (`python3 api/server.py` or `uvicorn ...`) while
> the launchd service is also active — port 8300 conflicts will cause both to crash-loop.
> Always use `launchctl stop` first, or `launchctl unload` to disable launchd management.

### OpenClaw plugin deployment

The plugin lives in `plugin/index.ts`. After making changes:

```bash
# Copy updated plugin to OpenClaw extension directory
cp plugin/index.ts ~/.openclaw/extensions/contextgraph/index.ts

# Restart OpenClaw gateway to load the new plugin
openclaw gateway restart
```

Toggle graph mode at runtime (in chat):
```
/graph on    # enable context graph
/graph off   # fall back to linear window
/graph       # show current status + API health
```

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

## Tests

```bash
python3 -m pytest tests/ -v
```

## Transition Roadmap

- [x] **Phase 1 — Passive Collection.** Harvest interactions, build the graph,
  evolve taggers. Corpus: 812+ interactions, 16 active tags.
- [x] **Phase 2 — Shadow Mode.** Validate graph assembly against linear baseline.
  Result: graph delivers more relevant context in fewer tokens.
- [x] **Phase 3 — Native Plugin (Plan of Record).** OpenClaw context engine plugin
  live. `/graph on|off` toggles at runtime. Sticky threads auto-activate on tool
  chains. Comparison logging writes `~/.tag-context/comparison-log.jsonl` every turn.
  See [`docs/PLAN_B_NATIVE_PLUGIN.md`](docs/PLAN_B_NATIVE_PLUGIN.md) for the
  full implementation plan.
- [ ] **Phase 4 — Graph-Primary.** After validation, graph becomes the default
  context engine. Linear window available as fallback.

## Documentation

- [`docs/CONTEXT_TRANSITION.md`](docs/CONTEXT_TRANSITION.md) — Design doc:
  the problem with linear context, the DAG vision, transition phases.
- [`docs/PLAN_B_NATIVE_PLUGIN.md`](docs/PLAN_B_NATIVE_PLUGIN.md) — Implementation
  plan for the native OpenClaw context engine plugin (Plan of Record).

## License

MIT
