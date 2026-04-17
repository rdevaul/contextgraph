# Context Graph — System Specification

Comprehensive internal specification for the Context Graph tag-based context
management system. This document covers architecture, data model, APIs,
deployment, and operational details.

**For getting started and operational overview, see [`README.md`](../README.md).**
**For the agent setup guide, see [`AGENT_SETUP.md`](../docs/AGENT_SETUP.md).**

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Components](#3-components)
4. [Data Model](#4-data-model)
5. [Tagging System](#5-tagging-system)
6. [Context Assembly](#6-context-assembly)
7. [Sticky / Pin Layer](#7-sticky--pin-layer)
8. [API Reference](#8-api-reference)
9. [Per-Channel Endpoints](#9-per-channel-endpoints)
10. [Quality Monitoring](#10-quality-monitoring)
11. [Channel Labels](#11-channel-labels)
12. [Configuration](#12-configuration)
13. [Deployment](#13-deployment)
14. [OpenClaw Plugin Integration](#14-openclaw-plugin-integration)
15. [Text Processing Utilities](#15-text-processing-utilities)
16. [Testing](#16-testing)

---

## 1. System Overview

### 1.1 What It Is

Context Graph is a **directed acyclic graph (DAG) context management system**
for LLM-based AI assistants. Every message/response pair is tagged with
contextual labels, forming a multi-tag membership graph ordered by time.
Instead of a flat sliding window, context is assembled from three layers:

- **Sticky** — explicitly pinned or automatically detected tool-chain messages
- **Recency** — most recent messages regardless of topic
- **Topic** — semantically relevant messages retrieved by inferred tags

### 1.2 Why It Exists

Standard LLM context management uses a **temporal sliding window**: pack the
most recent N messages until you hit a token budget. This has two problems:

1. **Old but relevant context gets lost.** Once a message ages past the window,
   it's gone — even if the user is asking about the same topic.
2. **Recent but irrelevant context takes up budget.** The latest exchanges
   might be about a completely different topic, wasting token budget that
   should be spent on material the LLM actually needs.

Context Graph solves this by treating context as a **tagged, queryable graph**
rather than a linear queue. The topic layer retrieves older but topically
relevant messages; the recency layer maintains conversational continuity; the
sticky layer preserves critical reference material.

### 1.3 Production Results

After 580+ retrieval turns at 4000-token budget (March 2026):

| Metric | Graph | Linear | Delta |
|--------|-------|--------|-------|
| Tokens/query | 3,423 | 3,717 | **11.8% savings** |
| Messages/query | 23.6 | 22.0 | +1.6 |
| Topic-retrieved msgs | 14.6 | 0 | **novel context** |
| Reframing rate | 1.5% | n/a | under 5% target |
| Cache hit rate | 99%+ | — | excellent |

The graph surfaces ~15 messages per query that the linear window would never
have included — older exchanges on the same topic that survived compaction.

---

## 2. Architecture

### 2.1 High-Level Data Flow

```
Incoming message
       │
       ▼
  ┌─────────────────┐
  │ strip_envelope  │  ← Remove channel metadata noise
  └────────┬────────┘
           │
           ▼
  ┌──────────────────┐
  │ extract_features │  ← NLP + structural features
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐     ┌─────────────────┐
  │  EnsembleTagger  │────►│  TagRegistry     │
  │  (fixed mode)    │     │  (tags.yaml│
  └────────┬─────────┘     │   + user tags)    │
           │               └─────────────────┘
           ▼
    inferred tags
           │
           ▼
  ┌──────────────────────┐
  │  ContextAssembler     │
  │  ├── StickyLayer     │  ← pin_manager.get_pinned_message_ids()
  │  ├── RecencyLayer    │  ← store.get_recent(N) by token budget
  │  └── TopicLayer      │  ← store.get_by_tag(tag) + IDF filter + scoring
  └──────────┬───────────┘
             │
             ▼
  Assembled context (oldest-first)
             │
             ├── Lazy summarization (>35% budget single msg)
             │
             ▼
  Returned to OpenClaw plugin as context
```

### 2.2 Component Relationships

```
api/server.py          FastAPI server, orchestrates flow
  ├── store              MessageStore (SQLite)
  ├── ensemble           EnsembleTagger (tagger wrapper)
  │     └── fixed_tagger   FixedTagger (keyword/pattern matching)
  ├── tag_registry       TagRegistry (system + user tags)
  ├── features           MessageFeatures (NLP extraction)
  ├── assembler          ContextAssembler (3-layer assembly)
  ├── sticky             StickyPinManager (pin lifecycle)
  ├── quality            QualityAgent (fitness scoring)
  ├── reframing          Reframing detection
  ├── summarizer         Lazy summarization
  └── utils/text         strip_envelope()
```

---

## 3. Components

### 3.1 `store.py` — MessageStore

**File:** `store.py`
**Tests:** `tests/test_store.py`

SQLite-backed store for message/response pairs with normalized tag associations
and pin management.

#### Message Dataclass

```python
@dataclass
class Message:
    id: str                          # UUID
    session_id: str
    user_id: str
    timestamp: float                 # Unix timestamp
    user_text: str
    assistant_text: str
    tags: List[str]
    token_count: int = 0
    external_id: Optional[str] = None    # OpenClaw AgentMessage.id
    summary: Optional[str] = None        # Summarized for large messages
    is_automated: bool = False           # cron/heartbeat/subagent turns
    channel_label: Optional[str] = None  # Per-channel isolation
```

#### MessageStore — Key Methods

| Method | Signature | Purpose |
|--------|-----------|---------|
| `add_message` | `(msg: Message) -> None` | Persist message + tags |
| `add_tags` | `(message_id, tags) -> None` | Add tags idempotently |
| `get_by_id` | `(message_id) -> Optional[Message]` | Fetch single message |
| `get_recent` | `(n, include_automated=False, channel_label=None) -> List[Message]` | N newest messages (optionally scoped) |
| `get_recent_by_session` | `(n, session_id) -> List[Message]` | Session-scoped recency |
| `get_by_tag` | `(tag, limit=20, include_automated=False) -> List[Message]` | Tag-filtered retrieval |
| `get_all_tags` | `() -> List[str]` | All distinct tags |
| `tag_counts` | `() -> Dict[str, int]` | {tag: message_count} |
| `get_by_external_id` | `(external_id) -> Optional[Message]` | Lookup by OpenClaw ID |
| `get_by_external_ids` | `(external_ids: List[str]) -> List[Message]` | Batch external ID lookup |
| `get_non_automated` | `(limit=1000) -> List[Message]` | Filter out cron/heartbeat |
| `count` | `(include_automated=False, channel_label=None) -> int` | Filtered count |
| `get_summary` / `set_summary` | | Lazy summarization storage |
| `get_channel_label_stats` | `() -> Dict[str, Dict]` | Stats per channel label |
| `channel_tag_counts` | `(channel_label=None) -> Dict[str, int]` | Tag frequencies scoped to channel |
| `channel_tag_count` | `(tag, channel_label=None) -> int` | Count of a single tag scoped to channel |
| `merge_channel_labels` | `(source_labels, target_label) -> Dict` | Merge operation + backup |

#### Threading Model

- Single shared SQLite connection with `check_same_thread=False`
- Reentrant lock (`RLock`) serializes all write operations
- WAL mode enables concurrent readers
- `busy_timeout=30000` prevents timeout under concurrent load

#### Database Migrations

Migrations are versioned in the `MIGRATIONS` dict and applied at startup:

| Version | Change |
|---------|--------|
| 2 | `external_id` column + index |
| 3 | `summary` column |
| 4 | `is_automated` column (default 0) |
| 5 | `channel_label` column + index |

Migration failures due to "duplicate column" are handled idempotently.

#### pin_manager (Module-Level)

The `sticky.py` module provides `StickyPinManager` — see [Section 7](#7-sticky--pin-layer).

---

### 3.2 `features.py` — Feature Extraction

**File:** `features.py`
**Tests:** `tests/test_features.py`

Extracts NLP and structural features from message pairs for tagging.

```python
@dataclass
class MessageFeatures:
    # Structural features
    user_text_len: int
    assistant_text_len: int
    turn_length: int
    has_code: bool
    has_urls: bool
    # NLP features (spaCy-based)
    user_entities: List[str]
    assistant_entities: List[str]
    user_noun_chunks: List[str]
    assistant_noun_chunks: List[str]
```

- `extract_features(user_text, assistant_text) -> MessageFeatures` — main entry point
- Uses spaCy (`en_core_web_sm`) for entity and noun chunk extraction
- Detects code blocks (triple backticks) and URLs via regex

---

### 3.3 `fixed_tagger.py` — FixedTagger

**File:** `fixed_tagger.py` (note: server imports `tagger.py` which wraps `FixedTagger`)
**Tests:** `tests/test_fixed_tagger.py`

Keyword and regex pattern-based tagger driven by YAML configuration.

#### TagSpec Dataclass

```python
@dataclass
class TagSpec:
    name: str
    keywords: List[str]         # Word-boundary keyword matches
    patterns: List[re.Pattern]  # Compiled regex patterns
    requires_all: bool          # AND vs OR matching logic
    confidence: float           # Pre-assigned confidence (default 1.0)
    enabled: bool
```

#### Matching Logic

- **Keywords:** Wrapped in `\b...\b` word boundaries for exact word matching
- **Patterns:** Full regex, case-insensitive with multiline flag
- **`requires_all=True`:** ALL keywords AND patterns must match
- **`requires_all=False`:** ANY single match fires the tag (short-circuit)

#### Hot-Reload

`FixedTagger` checks file modification times every `assign()` call. If the
system tag config or user tag file has changed since the last load, it
re-merges automatically — no restart needed. The `reload_interval` parameter
is accepted but the current implementation checks mtime directly.

#### User Tag Merging

When created via `FixedTagger.for_channel(channel_label)`:

1. System tags loaded from `tags.yaml`
2. User tags loaded from `~/.tag-context/tags.user/<channel_label>.yaml` (if exists)
3. User tags override system tags on name collision
4. Merged tag list is used for matching

---

### 3.4 `ensemble.py` — EnsembleTagger

**File:** `ensemble.py`
**Tests:** `tests/test_ensemble.py`

Weighted mixture model combining multiple tagging strategies.

```python
@dataclass
class TaggerEntry:
    tagger_id: str
    assign_fn: Callable[[MessageFeatures, str, str], TagAssignment]
    weight: float = 1.0   # updated by QualityAgent fitness scores

@dataclass
class EnsembleResult:
    tags: List[str]
    confidence: float
    per_tagger: Dict[str, List[str]]   # tagger_id → contributed tags
    tag_votes: Dict[str, float]        # tag → weighted vote score
```

#### Voting Algorithm

1. Each registered tagger runs on the input, producing a set of tags
2. Each tagger's weight is normalized: `weight / sum(all_weights)`
3. Each tag from a tagger adds the normalized weight to its vote score
4. Tags with `vote >= vote_threshold` (default 0.4) AND present in the
   registry's `get_active_tags()` set are accepted
5. Aggregate confidence = mean vote score of accepted tags

#### build_ensemble()

```python
def build_ensemble(
    mode: Optional[str] = None,
    quality_agent: Optional[QualityAgent] = None,
    vote_threshold: float = 0.4,
) -> EnsembleTagger:
```

| Mode | Taggers | Notes |
|------|---------|-------|
| `"fixed"` (default) | FixedTagger + baseline | No DEAP needed |
| `"hybrid"` | Fixed + GP tagger | Requires `deap` |
| `"gp-only"` | GP tagger only | Legacy, not recommended |

**Current production mode: `"fixed"`.** The GP tagger's deap dependency is broken,
and even in hybrid mode the GP's normalized vote cannot exceed the threshold under
default weights. Hybrid and gp-only modes are documented but not in active use.

---

### 3.5 `tagger.py` — Baseline Tag Assignment

**File:** `tagger.py`

Provides the baseline `assign_tags()` function and `TagAssignment` dataclass:

```python
@dataclass
class TagAssignment:
    tags: List[str]
    confidence: float
    rules_fired: List[str]
```

`CORE_TAGS` is defined here as the baseline set used when ensemble is not available.

The `_strip_metadata()` helper function removes channel metadata text (e.g.,
`message_id:`, `sender_id:` lines) from text before matching.

---

### 3.6 `assembler.py` — ContextAssembler

**File:** `assembler.py`
**Tests:** `tests/test_assembler.py`

Builds context windows from three layers packed to a token budget.

```python
@dataclass
class AssemblyResult:
    messages: List[Message]     # oldest-first, ready for LLM context
    total_tokens: int
    sticky_count: int           # from sticky layer
    recency_count: int          # from recency layer
    topic_count: int            # from topic layer
    tags_used: List[str]        # tags that contributed to retrieval
```

#### Budget Constants

| Constant | Default | Override |
|----------|---------|----------|
| `TOPIC_TAG_MAX_CORPUS_FREQ` | 0.30 | — |
| `MAX_SINGLE_MSG_BUDGET_FRACTION` | 0.35 | — |
| `STICKY_BUDGET_FRACTION` | 0.30 | `STICKY_BUDGET_FRACTION` env var |

#### Token Estimation

```python
def _estimate_tokens(msg: Message) -> int:
    if msg.token_count > 0:
        return msg.token_count
    words = len((msg.user_text + " " + msg.assistant_text).split())
    return max(1, int(words * 1.3))
```

---

### 3.7 `quality.py` — QualityAgent

**File:** `quality.py`
**Tests:** `tests/test_quality.py`

Tracks and scores tagger strategies using two proxy signals:

```python
@dataclass
class InteractionQuality:
    timestamp: float
    tagger_id: str
    context_density: float      # topic_count / (topic_count + recency_count)
    reframing_signal: float     # 0–1 from reframing detection
    composite: float            # weighted combination
```

#### Composite Score Formula

```
composite = 0.6 × context_density + 0.4 × (1.0 - reframing_rate)
```

- **Context density:** Higher is better — means topic retrieval is working
- **Reframing rate:** Lower is better — means users aren't re-establishing context
- Default window for mean: last 20 interactions

#### Persistence

State is saved to `data/quality-state.json` — keeps last 200 scores per tagger.

---

### 3.8 `sticky.py` — StickyPinManager

**File:** `sticky.py`
**Tests:** `tests/test_sticky.py`

In-memory pin manager with TTL-based expiry.

```python
@dataclass
class Pin:
    pin_id: str
    message_ids: List[str]
    pin_type: str              # "explicit", "tool_chain", "reference"
    reason: str
    ttl_turns: int
    turns_elapsed: int
    total_tokens: int
    created_at: float
```

Key methods:
- `add_pin(message_ids, pin_type, reason, ttl_turns, total_tokens) -> str`
- `remove_pin(pin_id) -> bool`
- `get_pinned_message_ids() -> List[str]`
- `get_active_pins() -> List[Pin]`
- `update_or_create_tool_chain_pin(message_ids, reason, total_tokens, ttl_turns)`
- `tick() -> List[str]` — expire stale pins, returns expired pin IDs

---

### 3.9 `tag_registry.py` — TagRegistry

**File:** `tag_registry.py`

Explicit-only tag registry with system and user registries.

```python
@dataclass
class TagMetadata:
    name: str
    state: str              # "core", "archived"
    first_seen: float
    last_seen: float
    hits: int
    promoted_at: Optional[float]
    archived_at: Optional[float]
```

#### System Tags

Loaded from `data/tags.yaml` on startup. States: `core`, `archived`.
No auto-discovery, no auto-promotion, no auto-demotion.

#### User Registries

Stored per-channel at `~/.tag-context/tags.user.registry/<label>.json`.
Created and modified only via explicit API calls. Isolated per channel label.

Key methods:
- `get_registry() -> TagRegistry` — global singleton (system registry)
- `get_user_registry(channel_label) -> Optional[TagRegistry]` — per-user
- `get_active_tags() -> Set[str]` — core tags only
- `get_active_tags_for_channel(channel_label) -> Set[str]` — system ∪ user

---

### 3.10 `reframing.py` — Reframing Detection

**File:** `reframing.py`

Detects when users are re-establishing lost context (a sign that retrieval
is failing). Returns a confidence score 0–1.

- `detect_reference(user_text) -> bool` — detects reference signals like "above",
  "earlier", "mentioned", "you said", etc.
- `reframing_rate(texts: List[str]) -> float` — reframing rate across a window

---

### 3.11 `summarizer.py` — Summarization

**File:** `summarizer.py`

Generates condensed summaries of oversized messages via Claude Haiku API
(configurable model).

- `summarize_message(msg: Message) -> str` — compresses a message for inclusion
- Used during context assembly when a single message exceeds 35% of the token budget
- Summary is cached on the Message via `store.set_summary()`

---

### 3.12 `logger.py` — Automated Turn Detection

**File:** `logger.py`

- `_is_automated_turn(user_text: str) -> bool` — detects cron jobs, heartbeats,
  and subagent turns by pattern matching on known identifiers

---

## 4. Data Model

### 4.1 SQLite Schema

```sql
-- Messages table
CREATE TABLE messages (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    timestamp       REAL NOT NULL,
    user_text       TEXT NOT NULL,
    assistant_text  TEXT NOT NULL,
    token_count     INTEGER NOT NULL DEFAULT 0,
    external_id     TEXT,                  -- v2 migration
    summary         TEXT,                  -- v3 migration
    is_automated    INTEGER NOT NULL DEFAULT 0,  -- v4 migration
    channel_label   TEXT                   -- v5 migration
);
CREATE INDEX idx_messages_timestamp ON messages(timestamp DESC);
CREATE INDEX idx_messages_external_id ON messages(external_id);
CREATE INDEX idx_messages_channel_label ON messages(channel_label);

-- Normalized tag associations
CREATE TABLE tags (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    PRIMARY KEY (message_id, tag)
);
CREATE INDEX idx_tags_tag ON tags(tag);

-- Schema versioning for migrations
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  REAL NOT NULL,
    description TEXT
);
```

### 4.2 JSON Registries

#### System Tag Definition
**Path:** `data/tags.yaml`

```json
{
  "tags": [
    {"name": "code", "state": "core"},
    {"name": "infrastructure", "state": "core"},
    ...
  ]
}
```

Only supports `core` and `archived` states. No auto-discovery — tags are
explicitly defined here and require a service restart to add/remove.

#### Quality Agent State
**Path:** `data/quality-state.json`

```json
{
  "fixed": {
    "tagger_id": "fixed",
    "scores": [
      {
        "timestamp": 1712345678.9,
        "tagger_id": "fixed",
        "context_density": 0.62,
        "reframing_signal": 0.0,
        "composite": 0.772
      }
    ]
  }
}
```

Keeps last 200 scores per tagger.

#### User Registries
**Path:** `~/.tag-context/tags.user.registry/<label>.json`

```json
{
  "message_count": 42,
  "tags": [
    {
      "name": "project-x",
      "state": "core",
      "first_seen": 1712345678.9,
      "last_seen": 1712350000.0,
      "hits": 15,
      "promoted_at": 1712345678.9,
      "archived_at": null
    }
  ]
}
```

#### Sticky Pin State

Pins are **in-memory only** — managed by `StickyPinManager` as a Python
singleton. They persist across `/assemble` calls within a single server process
but are lost on restart. The server recovers tool-chain pins by inspecting
`pending_chain_ids` on each `/assemble` call (with server-side fallback if the
plugin lost state).

### 4.3 Database Location

Default: `~/.tag-context/store.db`
Override: `CONTEXTGRAPH_DB_PATH` environment variable

Multiple agents can run concurrently with separate databases:
```
~/.tag-context/{AGENT_NAME}_messages.db
```
(set `CONTEXTGRAPH_AGENT_NAME` to namespace the path)

---

## 5. Tagging System

### 5.1 Production Configuration

The system runs in **`fixed` mode** only:

```
CONTEXTGRAPH_TAGGER_MODE=fixed
```

This uses two taggers in the ensemble:

1. **FixedTagger** — keyword/pattern matching from YAML/JSON config
2. **Baseline tagger** — `tagger.assign_tags()` wrapper

The `hybrid` and `gp-only` modes exist in code but are not used in production.
The GP tagger requires the `deap` library (installed but broken), and even
when functional the GP's weighted vote cannot exceed the default threshold
of 0.4 under normal weight distribution.

### 5.2 Tag Sources

#### System Tags
**Source:** `data/tags.yaml`
**Loaded at:** server startup
**Modification:** manual edit + restart

Currently ~50 core tags covering domains: `code`, `infrastructure`, `ai`,
`networking`, `openclaw`, `yapCAD`, `space-launch`, etc.

#### User Tags
**Source:** `~/.tag-context/tags.user.registry/<channel_label>.json`
**Creation:** explicit via `/tags user add` command (API: `POST /tags/user/<name>/add`)
**Scope:** isolated per channel label

User tags **extend** (not replace) the system tag set. Both are used during
tagging. The `TagRegistry.get_active_tags_for_channel()` method unions system
and user active tags.

### 5.3 FixedTagger Matching

```python
def _matches(spec: TagSpec, combined: str) -> bool:
    # 1. Keyword matching with word boundaries: r"\b{kw}\b"
    # 2. Pattern matching with compiled regex (case-insensitive, multiline)
    # 3. If requires_all: ALL keywords AND patterns must hit
    # 4. If not requires_all: ANY single match fires the tag
```

Matching is case-insensitive. Combined text (user + assistant) is lowercased
before matching. Channel metadata is stripped via `_strip_metadata()` before
matching.

### 5.4 Tag Pruning in Ensemble

Tags accepted by the ensemble must:
1. Receive weighted votes >= `vote_threshold` (default 0.4)
2. Be in the registry's `get_active_tags()` set (core state)

This prevents unknown or archived tags from contaminating context retrieval.

---

## 6. Context Assembly

### 6.1 Three-Layer Model

`ContextAssembler.assemble()` builds context in three phases:

#### Layer 1: Sticky (up to 30% of budget)

```python
sticky_budget = int(token_budget * sticky_budget_fraction)  # default: 0.3 = 30%
```

Fetched from `pin_manager.get_pinned_message_ids()`. Messages are looked up
by external_id first (OpenClaw IDs), falling back to internal ID. Each message
is individually cost-checked against the sticky budget.

#### Layer 2: Recency (25% of remaining budget)

```python
recency_budget = int(remaining_budget * 0.25)  # 25% whether sticky is active or not
```

Most recent non-duplicate messages. The first recency message is always
included regardless of the per-layer budget (safety valve).

#### Layer 3: Topic (rest of remaining budget)

```python
topic_budget = remaining_budget - recency_budget  # 75%
```

Messages retrieved by inferred tags, deduplicated against sticky + recency.

### 6.2 Topic Retrieval Pipeline

```
inferred_tags
      │
      ▼
[IDF filtering] — skip tags appearing in >30% of corpus
      │
      ▼
[tag retrieval] — store.get_by_tag(tag, limit=50) for each useful tag
      │
      ▼
[deduplication] — skip messages already in sticky ∪ recency
      │
      ▼
[scoring] — tag_score × 2 + recency_score
  tag_score    = number of query tags this message matches
  recency_score = 2^(-age_days / 30)  (exponential decay over ~30 days)
      │
      ▼
[budget packing] — fit messages to topic_budget, largest first by score
```

#### Scoring Formula

```python
def _score(m: Message) -> float:
    age_days = max(0, (now_ts - m.timestamp) / 86400)
    recency_score = 2 ** (-age_days / 30)
    tag_score = tag_hit_count.get(m.id, 1)
    return tag_score * 2 + recency_score
```

Messages matching more of the query tags rank higher. A fresh message matching
1 tag scores ~3.0; a week-old message matching 3 tags scores ~8.0. Tag
relevance is weighted **2× over recency** to prevent the staircase pattern
(where the topic layer just returns the N newest messages regardless of
semantic fit).

### 6.3 IDF Filtering

Tags appearing in more than 30% of the corpus are treated as **stop words**:

```python
TOPIC_TAG_MAX_CORPUS_FREQ = 0.30
```

Tags exceeding this threshold are excluded from topic retrieval because they
return nearly the entire corpus, blowing the token budget on low-relevance
messages.

**Fallback for small corpora:** If every inferred tag exceeds the threshold
(small corpus), the system sorts tags by ascending frequency and keeps the
bottom half.

Corpus size uses `store.count()` (actual non-automated message count), not
`max(tag_counts.values())`. The max-tag-count proxy was replaced because in
multi-channel stores the most-frequent system tag (`openclaw`) was on only
a subset of messages, artificially shrinking the denominator and causing
legitimate tags to be filtered as stop words (e.g. `voice-pwa` showed 34.7%
with max-tag denominator but should have been 12.0%).

### 6.4 Oversized Message Handling

No single message may exceed `MAX_SINGLE_MSG_BUDGET_FRACTION` (35%) of the
total token budget. When a message exceeds this:

1. Check for a cached `summary` on the message
2. If none, generate one via `summarize_message()` (Claude Haiku API)
3. Cache the summary on the message
4. Replace the message with: first 200 chars of user text + summary
5. If summarization fails, skip the message entirely

### 6.5 Output Order

All assembled messages are sorted **oldest-first** (ascending timestamp) for
natural reading order in the LLM context.

---

## 7. Sticky / Pin Layer

### 7.1 Overview

The sticky layer ensures that explicitly pinned turns remain in context
regardless of recency or topic score. Managed by `StickyPinManager` in
`sticky.py`.

**Important pins are in-memory only** — they do not have a database table.
They survive within a single server process lifetime but are lost on restart.
The server compensates by re-creating tool-chain pins from `pending_chain_ids`
on each `/assemble` call.

### 7.2 Pin Types

| Type | Trigger | TTL | Description |
|------|---------|-----|-------------|
| `"explicit"` | User `/pin` command | user-specified (default: 20 turns) | Manually pinned messages |
| `"tool_chain"` | Auto-detect tool use | 10 turns | Active tool-call chains |
| `"reference"` | Reference detection | 5 turns | Detected reference to prior work |

### 7.3 Pin Lifecycle

1. **Creation:** `pin_manager.add_pin()` or `update_or_create_tool_chain_pin()`
2. **Retention:** Pins are returned by `get_pinned_message_ids()` on each
   `/assemble` call
3. **Expiry:** `pin_manager.tick()` is called on each `/assemble` call;
   pins exceeding `ttl_turns` are removed and returned as expired IDs
4. **Removal:** Explicit `pin_manager.remove_pin(pin_id)`

### 7.4 Tool-Chain Auto-Pinning

On each `/assemble` call with `tool_state.last_turn_had_tools=True`:

1. If `pending_chain_ids` is non-empty: pin those specific messages
2. If empty but tools were active (server-side fallback):
   - Pin the 5 most recent messages (or 5 for the session if `session_id` provided)
   - Reason: "Active tool chain (server-side fallback: chain IDs lost on restart)"

### 7.5 Reference Detection

If `detect_reference(request.user_text)` returns true (user references prior
work), the server auto-pins the 5 most recent messages as a heuristic to preserve
the referenced context.

### 7.6 Sticky Budget

```
STICKY_BUDGET_FRACTION = 0.3  (default, 30% of token_budget)
```

When the sticky layer has entries, the remaining budget is split:
- Recency: 25% of remaining
- Topic: 75% of remaining

When the sticky layer is empty:
- Recency: 25% of total
- Topic: 75% of total

(The recency percentage is the same in both cases; the difference is whether
sticky consumes its budget first.)

### 7.7 Ingest-Side Degenerate Filtering

On `/ingest`, messages that are too short (<10 characters excluding whitespace)
or have excessive repetition (>40% duplicate 4-grams) are rejected. This
prevents noise from degrading tag quality and retrieval relevance.

---

## 8. API Reference

Base URL: `http://localhost:8302`
Framework: FastAPI (uvicorn)
All endpoints return JSON unless noted.

### 8.1 Core Endpoints

#### POST `/ingest`

Ingest a user/assistant message pair into the graph.

**Request:**
```json
{
  "session_id": "session-uuid",
  "user_text": "How do I fix the gateway?",
  "assistant_text": "To fix the gateway, run...",
  "timestamp": 1712345678.9,
  "user_id": "glados-rich",
  "external_id": "msg-abc123",
  "id": "api-1712345678.9",
  "channel_label": "telegram:rocket-team"
}
```

**Response:**
```json
{
  "ingested": true,
  "tags": ["networking", "infrastructure", "debugging"]
}
```

**Behavior:**
1. Strips channel metadata envelope from `user_text`
2. Sanitizes prompt injection patterns (replaces with `[REDACTED]`)
3. Auto-detects automated turns (cron/heartbeat/subagent) — skips tagging
4. Runs ensemble tagging on non-automated turns
5. Persists message to SQLite
6. Starts background summarization thread if token count > `SUMMARIZE_THRESHOLD` (default 2000)

---

#### POST `/assemble`

Assemble context for an incoming message.

**Request:**
```json
{
  "user_text": "What was the propulsion design decision?",
  "tags": ["space-launch", "rocket-design"],
  "token_budget": 4000,
  "tool_state": {
    "last_turn_had_tools": true,
    "pending_chain_ids": ["msg-abc", "msg-def"]
  },
  "session_id": "session-uuid"
}
```

**Response:**
```json
{
  "messages": [
    {"id": "...", "user_text": "...", "assistant_text": "...", "tags": ["..."], "timestamp": 1712345678}
  ],
  "total_tokens": 3421,
  "sticky_count": 2,
  "recency_count": 9,
  "topic_count": 14,
  "tags_used": ["space-launch", "rocket-design"],
  "expired_pins": []
}
```

**Behavior:**
1. Calls `pin_manager.tick()` to expire stale pins
2. If `tool_state.last_turn_had_tools`: auto-creates/extends tool-chain pins
3. If reference detected: auto-pins recent work thread
4. Strips envelope from query text
5. Runs ensemble tagging if no tags provided
6. Builds context via three-layer assembly
7. Returns assembled messages oldest-first

---

#### POST `/tag`

Tag a message pair (standalone, no ingestion).

**Request:**
```json
{  "user_text": "How does the tagger work?",
  "assistant_text": "The FixedTagger loads tags from tags.yaml..."
}
```

**Response:**
```json
{
  "tags": ["tagging", "system"],
  "confidence": {"tag_name": 0.85, ...},
  "per_tagger": {"fixed:tag_name": true, ...}
}
```

**Behavior:** Stands up a `TagRequest`, extracts features, runs `ensemble.assign()`, returns tags without persisting. Useful for testing new tag definitions.

---

#### GET `/health`

Service health check.

**Response:**
```json
{
  "status": "ok",
  "messages_in_store": 12345,
  "tags": ["ai", "infrastructure", "voice-pwa", ...],
  "engine": "contextgraph"
}
```

---

#### GET `/quality`

Retrieval quality metrics — the health check that tells you if the graph is actually working.

**Response:**
```json
{
  "zero_return_rate": 0.04,
  "avg_topic_messages": 4.2,
  "tag_entropy": 3.1,
  "top_tags": [{"tag": "ai", "count": 234, "corpus_pct": 0.18}, ...],
  "alert": false
}
```

**Fields:**

| Field | Description |
|---|---|
| `zero_return_rate` | Fraction of recent turns returning 0 graph messages. >0.25 = retrieval degraded. |
| `avg_topic_messages` | Average topic-layer message count across recent turns. |
| `tag_entropy` | Shannon entropy of tag distribution. <2.0 = over-generic tags. |
| `top_tags` | Most frequent tags with corpus frequency. |
| `alert` | `true` if quality is likely degraded. |

**Behavior:** Reads last 50 entries from `~/.tag-context/comparison-log.jsonl`. Filters to genuine retrieval turns only (excludes reference-only and recency-only turns).

---

#### GET `/metrics`

Quality/performance metrics broken down by tagger.

**Response:**
```json
{
  "quality_stats": {"fixed:base": {"fitness": 0.0, "mean_density": 0.04, "mean_reframing": 0.12}},
  "tagger_fitness": {"fixed:base": 1.0}
}
```

---

#### GET `/tags`

Returns system and user tags with metadata for the plugin's `/tags` command.

**Response:**
```json
{
  "system_tags": [{"name": "ai", "state": "core", "hits": 234, "corpus_pct": 0.18}],
  "user_tags": [{"name": "rocket-design", "state": "candidate", "hits": 12, "channel": "glados-rich"}]
}
```

**Behavior:** Aggregates `data/tags.yaml` with all `~/.tag-context/tags.user.registry/*.json` files. Only surfaces user tags with actual activity (hits > 0 or non-core state).

---

#### POST `/compare`

Assemble context with both graph and linear methods for comparison logging. Writes to `~/.tag-context/comparison-log.jsonl` for dashboard metrics.

**Request:** Same as `/assemble`.
**Response:** `{"graph_assembly": {...}, "linear_assembly": {...}}`

---

#### GET `/comparison-log`

Returns recent comparison log entries (default: last 50).

#### GET `/comparison-stats`

Aggregated statistics from the comparison log: `total_turns`, `avg_graph_tokens`, `avg_linear_tokens`, `token_savings_pct`, `cache_hit_rate`, `quality_alert_turns`.

---

#### GET `/dashboard`

Returns the HTML dashboard page (`api/dashboard.html`). Also available at `GET /`.

### 8.2 Registry Endpoints

#### GET `/registry`

Returns current tag registry state (core/candidate/archived tags with metadata).

#### POST `/registry/promote`

Force-promote a candidate tag to core. Query param `tag_name=rocket-design`.

**Response:** `{"success": true, "message": "Tag 'rocket-design' promoted to core"}`

#### POST `/registry/demote`

Force-archive a core tag. Query param `tag_name=old-project`.

**Response:** `{"success": true, "message": "Tag 'old-project' archived"}`

#### POST `/registry/tick`

DEPRECATED — no longer performs promotion/demotion. Returns a message confirming explicit-only mode.

### 8.3 Pin Endpoints

#### POST `/pin`

Create an explicit pin for specific messages.

**Request:**
```json
{"message_ids": ["msg-abc", "msg-def"], "reason": "Requirements doc", "ttl_turns": 30}
```

**Response:** `{"success": true, "pin_id": "pin-uuid-...", "message": "Created pin with 2 messages"}`

#### POST `/unpin`

Remove a pin by ID. Request: `{"pin_id": "pin-uuid-..."}`

**Response:** `{"success": true, "message": "Pin pin-uuid-... removed"}`

#### GET `/pins`

List all active pins with status (pin_id, pin_type, message_ids, reason, ttl_turns, turns_elapsed, turns_remaining, total_tokens, created_at).

### 8.4 Admin Endpoints

#### POST `/admin/merge-channel-labels`

Merge source channel_label values into a target label. Always dry-run first. Creates a timestamped DB backup before live execution.

**Request:** `{"source_labels": ["994902066"], "target_label": "glados-rich", "dry_run": true}`
**Note:** After merge, run `POST /admin/retag` to rebuild tags on merged messages.

#### POST `/admin/merge-all-channel-labels`

Nuclear option — merge ALL non-null, non-target labels into target.

#### POST `/admin/retag`

Re-run tagging on existing messages (after merges or tag config changes). If `message_ids` provided, only those messages. If omitted, the N most recent non-automated messages (default limit 100).

**Warning:** CPU-intensive at scale — each message requires full ensemble tagger evaluation.

#### GET `/admin/channel-labels`

List all channel labels with counts and session counts. Non-destructive, safe to call anytime.

---

### 8.5 Per-Channel Endpoints (Dashboard Support)

These endpoints expose per-channel data so the dashboard can scope statistics
and assembly previews to a specific user.

#### GET `/channels`

List all channel labels with message counts, session counts, and tag counts.
Drives the channel selector dropdown in the dashboard.

**Method:** `GET`
**Auth:** none (internal API)

**Response:**
```json
{
  "channels": {
    "glados-rich": {"message_count": 2160, "session_count": 487, "tag_count": 45},
    "glados-dana": {"message_count": 412, "session_count": 98, "tag_count": 28},
    "glados-terry": {"message_count": 308, "session_count": 87, "tag_count": 25}
  }
}
```

Channels with `null` label are omitted (these are pre-channel_label legacy data).

#### GET `/quality/channel/{channel_label}`

Quality metrics scoped to a specific channel.

**Method:** `GET`

**Returns:** Same shape as `/quality` but computed from that channel's
recent messages only (`zero_return_rate`, `avg_topic_messages`,
`tag_entropy`, and per-channel `top_tags`).

#### GET `/tags/channel/{channel_label}`

Tag frequency distribution scoped to a specific channel.

**Method:** `GET`

**Returns:** `{"channel": <label>, "total_messages_tagged": int, "tags": [{"name": str, "count": int, "pct": float}]}`

#### POST `/compare/channel/{channel_label}`

Run the comparison endpoint graph-assemble vs linear-window with scope
limited to a specific channel's data.

**Method:** `POST`
**Body:** Same as `/compare` request.

**Returns:** Same as `/compare`, but topic retrieval only uses messages
matching the given `channel_label`.

---

## 10. Channel Labels — Cross-Channel User Identity

### 9.1 Problem

Each messaging platform assigns unique user IDs (Telegram numeric, Discord snowflake, etc.). The same person on multiple platforms creates separate identities, fragmenting per-user tag profiles.

### 9.2 Solution

A `channel_labels.yaml` config file maps platform-specific sender IDs to a canonical username:

```yaml
labels:
  "994902066": "glados-rich"
  "510637988242522133": "glados-rich"
  "900606288": "glados-dana"
  "7686402653": "glados-terry"
```

### 9.3 How It Works

1. **Lookup:** On `/ingest`, the server checks `channel_labels.yaml` for the sender ID
2. **Mapping:** If found, the canonical label replaces the raw sender ID
3. **Fallback:** If not found, the raw sender ID is used
4. **Retrieval:** Tags are scoped to the canonical label

### 9.4 Migration

```bash
# Dry run first
curl -X POST http://localhost:8302/admin/merge-channel-labels \
  -d '{"source_labels": ["994902066"], "target_label": "glados-rich", "dry_run": true}'

# Execute
curl -X POST http://localhost:8302/admin/merge-channel-labels \
  -d '{"source_labels": ["994902066"], "target_label": "glados-rich", "dry_run": false}'

# Retag merged messages
curl -X POST http://localhost:8302/admin/retag -d '{"limit": 500}'
```

See `docs/MERGE_CHANNELS.md` for complete admin guide.

---

## 10. Deployment

### 10.1 Launchd Services

| Service | Label | Port | Proxy |
|---|---|---|---|
| tag-context (API) | `com.glados.tag-context` | 8302 | Caddy: 8443→8302 (HTTPS) |

**Commands:**
```bash
launchctl list | grep tag-context        # Status
launchctl stop com.glados.tag-context    # Stop
launchctl start com.glados.tag-context   # Start
tail -f /tmp/tag-context.log             # Logs
```

### 10.2 Health Checks

| Check | Endpoint | Expected |
|---|---|---|
| Service up | `GET /health` | `{"status": "ok"}` |
| Quality | `GET /quality` | `{"alert": false, "zero_return_rate": < 0.25}` |

### 10.3 OpenClaw Plugin Integration

The contextgraph plugin lives in `~/.sybilclaw/plugins/contextgraph/`. On each turn:

1. **Ingest** — POST `/ingest` with user/assistant message pair
2. **Assemble** — POST `/assemble` with user text, tags, token budget, tool state
3. **Compare** (optional) — POST `/compare` for side-by-side evaluation

### 10.4 Data Directory

All persistent state lives in `~/.tag-context/`:

```
~/.tag-context/
├── context.db                    # SQLite store (WAL mode)
├── channel_labels.yaml           # Platform ID → canonical label
├── comparison-log.jsonl          # Comparison mode turn logs
├── tags.user.registry/
│   ├── glados-rich.json
│   ├── glados-dana.json
│   └── glados-terry.json
└── backups/                      # DB backups before admin ops
```

---

## 11. Configuration

### 11.1 Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONTEXTGRAPH_DB_PATH` | `~/.tag-context/context.db` | SQLite database path |
| `CONTEXTGRAPH_TAGS_CONFIG` | `data/tags.yaml` | System tags file |
| `CONTEXTGRAPH_TAGGER_MODE` | `"fixed"` | Tagger mode. Only "fixed" works. |
| `STICKY_BUDGET_FRACTION` | `"0.3"` | Fraction of token budget for pinned messages |

### 11.2 System Tags

`data/tags.yaml` — core/candidate/archived tags with keywords.

### 11.3 User Tag Registries

Per-user JSON in `~/.tag-context/tags.user.registry/`. Same schema as system tags. Loaded on each request — edits take effect immediately.

### 11.4 Channel Labels

Platform ID canonicalization. See Section 9. Created automatically by merge admin endpoint or manual editing.

---

*Last revised: 2026-04-12. This spec is the authoritative reference for internals — README.md is the quickstart.*
