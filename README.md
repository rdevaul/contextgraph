# tag-context

Tag-based hierarchical context management for LLMs.

**Status:** Prototype v0.1 — core scaffold complete, GP/quality agent in v0.2

## Problem

Standard LLM context management is temporal (flat sliding window). Compaction
blends unrelated topics into noise, and old-but-relevant context gets lost while
recent-but-irrelevant context takes up token budget.

## Approach

Every message/response pair is tagged with contextual labels. Context assembly
pulls from two layers:

1. **Recency layer** (25% of budget) — most recent messages regardless of tag
2. **Topic layer** (75% of budget) — messages retrieved by inferred tags for the
   incoming message, deduplicated against the recency layer

The underlying structure is a **DAG** (directed acyclic graph): time-ordered,
multi-tag membership, no cycles.

## Architecture

```
Incoming message
       │
       ▼
  FeatureExtractor ──► Tagger ──► inferred tags
                                        │
                                        ▼
                              ContextAssembler
                              ├── RecencyLayer (MessageStore.get_recent)
                              └── TopicLayer  (MessageStore.get_by_tag × tags)
                                        │
                                        ▼
                              Assembled context (oldest-first)
```

### Future (v0.2)
- **Tagger family** — multiple taggers evolved via genetic programming (DEAP)
- **Quality agent** — scores tagging strategies on: context density + user reframing frequency
- **Mixture model** — weighted ensemble of top-performing taggers + pruning step
- **Compaction** — tag-conditioned summarization of distant messages; lossless archival

Full design doc: `~/.openclaw/workspace/projects/tag-context-system.md`

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

# Show a specific message
python3 cli.py show <message_id>
```

## Tests

```bash
eval "$(pyenv init -)"
python3 -m pytest tests/ -v
```

## Files

| File | Purpose |
|---|---|
| `store.py` | SQLite MessageStore + tag index |
| `features.py` | Feature extraction from message text |
| `tagger.py` | Structured-program tagger (v0 baseline) |
| `assembler.py` | Context assembly policy |
| `cli.py` | CLI for testing |
| `tests/` | pytest suite |
