---
*Prepared by **Agent: Mei (梅)** — PhD candidate, Tsinghua KEG Lab. Specialist in memory systems, inference optimization, and distributed AI architecture.*
*Running: anthropic/claude-opus-4-5*

*Human in the Loop: Garrett Kinsman*

---

# ContextGraph ↔ OpenClaw Integration Spec
*v1-2026-03-19*

## BLUF

This patch bridges file-based memory (`memory/daily/`, `memory/projects/`, etc.) into ContextGraph's tag-indexed DAG, and provides a Python API for assembling context at session start. It's a working fix while Rich's improved rolling context system is in development.

**What this enables:** OpenClaw can call `context_injector.py` before session start to get a dynamically-assembled context block based on the incoming query, rather than relying solely on the static `MEMORY.md` injection.

---

## What Was Built

### 1. `scripts/memory_harvester.py`

**Purpose:** Crawl memory directories and index files into ContextGraph.

**What it does:**
- Crawls: `memory/daily/`, `memory/projects/`, `memory/decisions/`, `memory/contacts/`
- Reads YAML frontmatter tags (e.g., `tags: [maxrisk, trading, options]`)
- Creates ContextGraph Messages with:
  - `user_text` = `[category] Title` (searchable query representation)
  - `assistant_text` = file content (what gets retrieved)
  - `tags` = frontmatter tags + auto-inferred tags from tagger.py
  - `external_id` = `memory-file:{relative_path}` (for idempotent updates)
- Uses content hash to skip unchanged files (incremental updates)
- Designed for cron (nightly) or on-demand

**Usage:**
```bash
# Test run (no writes)
python3 scripts/memory_harvester.py --dry-run --verbose

# Full harvest
python3 scripts/memory_harvester.py

# Force re-index all
python3 scripts/memory_harvester.py --force
```

**State file:** `data/memory-harvester-state.json`

### 2. `scripts/context_injector.py`

**Purpose:** Assemble context from ContextGraph for session injection.

**What it does:**
- Takes incoming query (user's first message)
- Infers tags using existing tagger.py
- Calls ContextAssembler with configured token budget
- Returns formatted markdown block suitable for system prompt injection

**CLI usage:**
```bash
# Query test
python3 scripts/context_injector.py "what's the maxrisk project status?"

# With custom budget
python3 scripts/context_injector.py --budget 1500 "memory architecture"

# JSON output for API integration
python3 scripts/context_injector.py --json "trading research"
```

**Python API:**
```python
from scripts.context_injector import assemble_context, assemble_for_session

# Simple: get formatted context block
context_block = assemble_context("user query", token_budget=2000)

# Full: get block + metadata
result = assemble_for_session("user query")
# result = {
#     "context_block": str,     # markdown for injection
#     "tokens": int,            # estimated tokens used
#     "message_count": int,     # messages retrieved
#     "tags": ["tag1", "tag2"], # tags that matched
#     "source": "contextgraph",
# }
```

**Output format:**
```markdown
## Retrieved Context

*Assembled by ContextGraph — 8 messages, ~1847 tokens*
*Query tags: [maxrisk, trading, options]*

### [2026-03-18] MaxRisk Project Status
*Tags: maxrisk, trading, options*

Current equity: $3,884.55. Focus on 30-45 DTE debit spreads...

### [2026-03-17] Trading Research Notes
*Tags: maxrisk, research*

Volume rotation strategy analysis...
```

---

## What This Doesn't Do (Gaps for Rich's System)

### 1. No Hook Into Injection Layer

This patch provides the **assembly function** but doesn't wire it into OpenClaw's actual injection layer. Someone needs to:

- Add a call to `assemble_for_session()` in the OpenClaw session bootstrap path
- Decide whether the result **replaces** MEMORY.md or **augments** it
- Handle the case where ContextGraph returns empty (fallback to MEMORY.md)

**Recommended integration point:** Wherever OpenClaw builds the system prompt at session start, add:

```python
from projects.contextgraph_engine.scripts.context_injector import assemble_for_session

# At session start, before building system prompt:
result = assemble_for_session(first_user_message)
if result["message_count"] > 0:
    system_prompt += "\n\n" + result["context_block"]
```

### 2. No Semantic Search Fallback

The current implementation uses **tag-based retrieval only**. If the user's query doesn't match any known tags, the topic layer returns empty.

**Rich's system should add:** Semantic similarity search (using nomic-embed-text or similar) as a fallback when tag retrieval returns few results.

### 3. No MEMORY.md Integration

This patch doesn't modify or replace MEMORY.md. The two systems are additive:
- MEMORY.md = static, manually curated, always injected
- ContextGraph = dynamic, auto-tagged, query-based

**For the static-overrides-dynamic problem:** Either:
- Keep MEMORY.md very slim (project status one-liners only)
- Have Rich's system generate MEMORY.md from ContextGraph at session start
- Replace MEMORY.md injection with ContextGraph injection entirely

### 4. No Real-Time Indexing

`memory_harvester.py` is batch-mode only. Changes to memory files aren't reflected until next harvest.

**For real-time:** Could add a file watcher (fswatch, watchdog) that triggers incremental indexing on file change.

### 5. No Sub-Agent Context Propagation

When the main session spawns a sub-agent, the sub-agent doesn't automatically get relevant context from ContextGraph. This is why Mei ran 41 min and the main session forgot what she was doing.

**Rich's system should address:** Context propagation to sub-agents, possibly via:
- Injecting a "task context" block when spawning
- Having sub-agents call `assemble_for_session()` with their task description

---

## Integration Checklist for Rich

### Phase 1: Harvest Pipeline
- [x] `memory_harvester.py` crawls memory directories
- [x] YAML frontmatter tags → ContextGraph DAG edges
- [x] Content hash for incremental updates
- [ ] Add to nightly cron (alongside existing `harvester.py`)

### Phase 2: Injection Layer
- [x] `context_injector.py` assembles context
- [x] Python API for integration
- [ ] Wire into OpenClaw session bootstrap
- [ ] Decide MEMORY.md relationship (replace vs. augment)

### Phase 3: Enhanced Retrieval (Rich's Improvements)
- [ ] Semantic search fallback when tag retrieval is sparse
- [ ] Cross-session context propagation for sub-agents
- [ ] Rolling window with recency decay
- [ ] Salience-weighted ranking across sources

---

## File Locations

| File | Purpose |
|------|---------|
| `scripts/memory_harvester.py` | Batch indexer for memory files |
| `scripts/context_injector.py` | Context assembly API |
| `data/memory-harvester-state.json` | Harvest state (files indexed, hashes) |
| `~/.tag-context/store.db` | ContextGraph SQLite database |
| `data/harvester-state.json` | Session harvester state (existing) |

---

## Testing

### Verify Memory Harvester
```bash
cd projects/contextgraph-engine

# Dry run to see what would be indexed
python3 scripts/memory_harvester.py --dry-run --verbose

# Actually harvest
python3 scripts/memory_harvester.py --verbose

# Check tag counts
python3 cli.py tags
```

### Verify Context Injector
```bash
# Query for a known topic
python3 scripts/context_injector.py "maxrisk project"

# Check retrieval stats
python3 scripts/context_injector.py --stats-only "memory architecture"

# JSON output
python3 scripts/context_injector.py --json "trading research"
```

### End-to-End Test
```bash
# 1. Harvest memory files
python3 scripts/memory_harvester.py

# 2. Query ContextGraph
python3 cli.py query "what's the maxrisk status?"

# 3. Get injectable context
python3 scripts/context_injector.py "what's the maxrisk status?"
```

---

## Architecture Notes

### Why Tag-Based + File-Based?

ContextGraph already handles interactive sessions via `harvester.py`. This patch adds file-based memory as a second source. Both flow into the same DAG:

```
┌──────────────────────┐     ┌──────────────────────┐
│ OpenClaw Sessions    │     │ Memory Files         │
│ (harvester.py)       │     │ (memory_harvester.py)│
└──────────┬───────────┘     └──────────┬───────────┘
           │                            │
           │  JSONL → Messages          │  .md → Messages
           │  auto-tags via tagger      │  frontmatter + auto-tags
           │                            │
           ▼                            ▼
     ┌─────────────────────────────────────┐
     │         ContextGraph DAG            │
     │  (SQLite: messages + tags tables)   │
     └─────────────────┬───────────────────┘
                       │
                       │ query → ContextAssembler
                       ▼
     ┌─────────────────────────────────────┐
     │      Assembled Context Block        │
     │  (recency layer + topic layer)      │
     └─────────────────────────────────────┘
                       │
                       │ context_injector.py
                       ▼
     ┌─────────────────────────────────────┐
     │     OpenClaw System Prompt          │
     │  (injected at session start)        │
     └─────────────────────────────────────┘
```

### Token Budget Allocation

Default: 2000 tokens for injected context

- Recency layer: 25% (~500 tokens) — most recent messages
- Topic layer: 75% (~1500 tokens) — tag-matched messages

This is tunable via `assemble_context(query, token_budget=N)`.

### External ID Convention

Memory files use `external_id = "memory-file:{relative_path}"` to enable:
- Idempotent re-indexing (update instead of duplicate)
- Tag updates without re-inserting messages
- Traceable source for debugging

---

## Known Issues

1. **Path sensitivity:** Harvester assumes workspace at `~/.openclaw/workspace`. If workspace moves, update `WORKSPACE` constant in `memory_harvester.py`.

2. **Tag canonicalization:** Frontmatter tags are passed through directly. If they don't match tags in `tag_registry.py`, they'll be indexed but may not participate in candidate promotion.

3. **Token estimation:** Uses word count × 1.3 heuristic. Actual tokenization depends on model. For accurate counts, integrate tiktoken or the model's tokenizer.

---

*End of spec. Questions → Garrett → Mei.*
