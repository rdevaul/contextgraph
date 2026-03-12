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

## Documentation

- [`docs/CONTEXT_TRANSITION.md`](docs/CONTEXT_TRANSITION.md) — Design doc:
  transitioning from linear context to graph-primary assembly. Covers the
  problem, transition phases, key design questions, and how this differs
  from standard RAG.

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

## Tests

```bash
python3 -m pytest tests/ -v
```

## Transition Roadmap

- [x] **Phase 1 — Passive Collection.** Harvest interactions, build the graph,
  evolve taggers. Corpus: 812+ interactions, 16 active tags.
- [x] **Phase 2 — Shadow Mode.** Validate graph assembly against linear baseline.
  Result: graph delivers more relevant context in fewer tokens.
- [ ] **Phase 3 — Hybrid Injection.** Inject tag-retrieved context as a preamble
  before the normal linear window. Lowest-risk integration point.
- [ ] **Phase 4 — Graph-Primary.** Graph assembler becomes the primary context
  source. Linear window shrinks to a small recency buffer.

## License

MIT
