# From Linear Window to Context Graph: A Transition Design Doc

*Written: 2026-02-27*
*Status: Design / In Progress*

---

## The Problem with Linear Context

Every LLM conversation today runs on a sliding window: recent messages in, oldest messages out. When the window fills, a compaction step summarizes and discards. This is operationally simple but has deep structural problems:

**Temporal proximity ≠ relevance.** The most useful context for a given query is often not the most recent — it's the most *topically related*. A conversation about voice PWA deployment from three days ago is far more useful for today's voice PWA question than yesterday's shopping list discussion, but the linear window doesn't know that.

**Compaction blends signal into noise.** When context is summarized, distinct topics get flattened into a single paragraph. The specificity that made the original conversation useful — the exact error message, the config file path, the design decision rationale — evaporates. Compaction is lossy by design.

**Cold-start problem on every session.** Each session wakes up knowing only what survived the last compaction. Background context about ongoing projects, preferences, and prior decisions has to be re-established from scratch or maintained in static files that grow unwieldy.

**Reframing cost.** Users who notice missing context spend tokens re-establishing it: "as I mentioned before...", "going back to the voice PWA...". This is waste — the information existed, it was just discarded.

---

## The Tag-Context Vision

The tag-context system treats conversation history as a **directed acyclic graph** (DAG) rather than a tape:

- Each interaction (user/assistant pair) is a **node**
- Nodes carry **tag memberships** (topic labels)
- Edges are temporal (time-ordered) and topical (shared tags)
- The graph grows continuously and is never discarded

Context assembly for a new query becomes a **retrieval problem**:

1. **Infer tags** for the incoming message (what topics does this touch?)
2. **Recency layer**: pull the N most recent interactions regardless of topic
3. **Topic layer**: retrieve the K most relevant interactions by tag match, deduplicated against recency
4. **Assemble**: merge, sort oldest-first, inject into context window

The result: a context window that contains *both* recent continuity *and* long-range topical relevance, assembled fresh for each query.

---

## Current State (Feb 2026)

### What's Built

| Component | Status | Notes |
|---|---|---|
| Interaction harvester | ✅ Running | Nightly at 2am, ~40-70 meaningful exchanges/day |
| MessageStore (SQLite) | ✅ Running | 159 messages tagged, growing daily |
| Rule-based tagger (v0) | ✅ Running | 20 tags, keyword/feature rules |
| GP tagger (v0.2) | ✅ Running | DEAP evolution, 16/20 tags with fitness >0.7 |
| Ensemble tagger | ✅ Running | Baseline + GP weighted vote |
| Context assembler | ✅ Built | RecencyLayer + TopicLayer, not yet injected |
| Quality agent | ✅ Built | Density + reframing metrics, not yet wired |
| Context injection | ❌ Not started | The final integration step |

### What the Nightly Pipeline Does

```
02:00 AM daily
  harvester.py  →  collect new interactions from main session
  evolve.py     →  retrain GP tagger on all interactions (pop=80, gen=30, ~5s)
  replay.py     →  retag all messages using baseline + GP ensemble
```

The graph is being built and improved nightly. It is not yet being *used* to assemble context.

---

## The Transition Path

### Phase 1 — Passive Collection (Current)

Harvest and tag interactions. Build the graph. Improve the tagger. No change to user experience.

The key output of this phase: a tagged corpus large enough to train a tagger that generalizes well. Current corpus: ~200 interactions, 20 tags. Target for stable baseline: ~1000 interactions.

*Estimated timeline: 2-3 weeks of continuous harvesting.*

### Phase 2 — Shadow Mode

Run the context assembler in parallel with the existing linear window. Log what it *would have* assembled for each query. Compare against what was actually in the linear window.

This is where the quality agent becomes active: measure context density (how much of the assembled context was topic-retrieved vs. recency-only) and reframing rate (how often the user re-establishes context that was available in the graph).

The goal is to verify that graph assembly produces better context before it's trusted with real queries.

*No user-facing change. Produces quality metrics and validates the assembler.*

### Phase 3 — Hybrid Mode

Inject tag-retrieved context as a **preamble** before the normal linear window. The linear window continues to operate as-is; the graph provides supplementary long-range context.

This is the lowest-risk integration point. If graph assembly is wrong or irrelevant, the linear window still provides coherent recent context. The preamble is bounded in size (e.g., 2000 tokens) and clearly delimited.

Format injected into context:
```
[RELEVANT PRIOR CONTEXT — retrieved by topic]
<assembled messages from graph, oldest-first>
[END PRIOR CONTEXT]

<normal linear context window>
<current query>
```

### Phase 4 — Graph-Primary Mode

The graph assembler becomes the primary context source. The linear window shrinks to a small recency buffer (last 3-5 exchanges) for conversational continuity. The bulk of the context budget goes to topically-retrieved material.

Steady-state: a session that can coherently discuss voice PWA architecture without needing three days of conversation in the window, because the most relevant prior voice-pwa-tagged exchanges are always available regardless of when they occurred.

---

## Key Design Questions

### Tag quality is everything

The system is only as good as the tagger. If `voice-pwa` tags fire on shopping discussions, the assembled context will be noise. The GP evolution loop + quality agent feedback is the improvement mechanism — but it requires a large enough corpus to generalize, and real quality signal beyond pseudo-labels from the baseline tagger.

**Near-term**: expand training corpus, add more discriminating features to the GP feature vector, wire quality agent fitness into evolution (currently using baseline pseudo-labels only).

**Long-term**: consider embedding-based retrieval as a complement to tag retrieval. Tags give interpretable, controllable retrieval; embeddings give finer-grained semantic similarity. The two are complementary.

### Context budget allocation

In Phase 3/4, how much of the context budget goes to graph-retrieved vs. recency? The current assembler uses 25% recency / 75% topic. This is a hypothesis requiring empirical validation via the quality agent.

Different query types likely want different ratios: a technical debugging question benefits from deep topic retrieval; a casual follow-up mostly needs recent continuity. A routing step that classifies query type before assembly may help.

### Session boundaries

The harvester currently pulls from a single session (`agent:main:main`). In a multi-session world (isolated cron jobs, sub-agents), cross-session retrieval may be relevant. The graph schema supports `session_id` per message — the assembler could optionally retrieve across sessions, but this requires careful thought about what should cross session boundaries and what shouldn't.

### Privacy and sensitivity

The graph accumulates everything. Some interactions involve sensitive personal details that shouldn't surface in all query contexts. A `sensitivity` tag or explicit exclude-list may be needed before Phase 3 deployment.

### Staleness and decay

Older interactions may become irrelevant or wrong (a deployment decision later reversed, a config that changed). Options:
- Time-decay weighting in retrieval (prefer recent within a tag)
- Explicit `superseded` status for contradicted interactions
- TTL-based pruning for low-signal tags after N days

---

## What Makes This Different from RAG

Standard Retrieval-Augmented Generation (RAG) retrieves from a static document corpus using embedding similarity. Tag-context differs in three important ways:

**1. Live corpus.** The retrieval source is conversation history, continuously updated. It's not a document store — it's a self-model of ongoing work.

**2. Learned tags.** Tags are evolved from the conversation itself via GP, not assigned by human curators or static embeddings. The tagger adapts to the specific topics and vocabulary of this user and this session over time.

**3. Endogenous feedback.** Quality signals come from the conversation itself. If context assembly is working, the user stops re-establishing context — that reframing rate drop directly improves the tagger via the quality agent. It's a closed feedback loop that doesn't exist in standard RAG.

In short: RAG retrieves from a fixed external corpus. Tag-context retrieves from a self-improving model of the conversation's own topic structure.

---

## Success Criteria

The transition to graph-primary context is successful when:

1. **Reframing rate < 5%** — fewer than 1 in 20 user messages re-establish context that was available in the graph
2. **Context density > 60%** — more than 60% of assembled context comes from topic retrieval, indicating the tagger is surfacing genuinely relevant material
3. **No coherence regressions** — conversations don't feel disjointed from graph-injected context
4. **Compaction events decrease** — the graph reduces reliance on lossy summarization by keeping long-range context accessible without it

---

## Related Files

- `README.md` — architecture overview
- `assembler.py` — context assembly (recency + topic layers)
- `quality.py` — quality agent (density + reframing metrics)
- `gp_tagger.py` — GP evolution harness
- `scripts/harvester.py` — nightly interaction collection
- `scripts/evolve.py` — nightly GP retraining
- `scripts/replay.py` — ensemble retagging
- `~/.tag-context/store.db` — the live message store
- `data/gp-tagger.pkl` — current evolved tagger model
