# User-Scoped Tags — Design Spec v1

**Author:** GLaDOS (spec) + Rich (architecture direction)  
**Date:** 2026-03-27  
**Status:** Draft — ready for implementation

---

## Overview

Extend the context graph tagging system to support per-user tags alongside system-wide tags. User tags are scoped to a specific user's content and retrieval context, enabling personalized context assembly without cross-contaminating other users' sessions.

## Goals

1. **Separate system tags from user tags** — system tags (`code`, `weather`, `travel`) ship with the system; user tags (`zheng-survey`, `skill-registry`) are personal
2. **Scope retrieval** — user tags only match messages produced by that user (filtered by `channel_label`)
3. **Multi-user safe** — Rich's user tags don't affect Dana's or Terry's context assembly
4. **Interactive management** — API endpoints first, `/tag` command later via contextgraph plugin

## Architecture

### File Layout

```
~/.tag-context/
├── store.db                        # message store (existing)
├── tag_registry.json → data/       # system tag registry (existing, stays in repo)
├── tags.user/                      # NEW — per-user tag directories
│   ├── rich.yaml                   # Rich's personal tags
│   ├── dana.yaml                   # Dana's personal tags (future)
│   └── terry.yaml                  # Terry's personal tags (future)
└── tags.user.registry/             # NEW — per-user tag registries
    ├── rich.json
    ├── dana.json
    └── terry.json
```

System tags remain in the repo at `tags.yaml` and `data/tag_registry.json`.

User tag files follow the same YAML format as `tags.yaml`:
```yaml
# ~/.tag-context/tags.user/rich.yaml
version: 1
tags:
  - name: zheng-survey
    description: Zheng consulting survey platform project
    keywords:
      - zheng
      - lilyzhengfair
      - culture assessment
    confidence: 1.0
```

### Channel Label Population

**Current state:** The OpenClaw contextgraph plugin (`engine.ts`) does NOT pass `channel_label` during ingest. All 907 non-automated messages have `channel_label=NULL`.

**Fix required in `engine.ts`:**

The `ingest()` and `afterTurn()` methods need to extract the channel label from the OpenClaw session context and pass it to the API. The channel label should identify the user, e.g.:
- `"rich"` for Rich's direct Telegram session
- `"dana"` for Dana's direct session  
- `"household"` for the household group
- `"cron"` for cron jobs

The label should come from the agent ID or session binding. The plugin has access to `params.sessionId` which encodes the agent (e.g. `agent:main:main` → `"rich"`, `agent:glados-dana:...` → `"dana"`).

**Mapping logic (in engine.ts):**
```typescript
function inferChannelLabel(sessionId: string): string {
  if (sessionId.includes('glados-dana')) return 'dana';
  if (sessionId.includes('glados-terry')) return 'terry';
  if (sessionId.includes('glados-household')) return 'household';
  if (sessionId.includes('cron:')) return 'cron';
  // Default: main agent = Rich
  return 'rich';
}
```

**Backfill:** A one-time migration script should set `channel_label` on existing messages based on `session_id` patterns.

### FixedTagger Refactor

**Current:** `FixedTagger` loads a single `tags.yaml` file.

**New:** `FixedTagger` accepts an optional `user_tags_path` parameter. When assembling:
1. Load system tags from `tags.yaml` (as today)
2. Load user tags from `~/.tag-context/tags.user/<channel_label>.yaml` if it exists
3. Merge both tag sets (user tags override system tags on name collision)
4. Return combined results

**API server change:** The `/tag`, `/assemble`, and `/compare` endpoints need to accept an optional `channel_label` parameter so they can load the right user tags.

### Tag Registry Scoping

**System registry:** `data/tag_registry.json` — unchanged, manages system tag lifecycle.

**User registries:** `~/.tag-context/tags.user.registry/<label>.json` — same format, manages user tag lifecycle independently.

When determining `active_tags` in the ensemble, merge system active + user active (for the current user).

### Assembly Scoping

When the assembler retrieves messages by user tag, it should filter by `channel_label`:

```python
# In assembler.py, for user-scoped tags:
for tag in user_tags:
    for msg in self.store.get_by_tag(tag, limit=20, channel_label=user_label):
        ...
```

This requires a new `channel_label` parameter on `get_by_tag()`:

```python
def get_by_tag(self, tag: str, limit: int = 20, 
               include_automated: bool = False,
               channel_label: str = None) -> List[Message]:
    """If channel_label is set, only return messages with that label."""
```

System tags retrieve from ALL non-automated messages (no channel filter). User tags only retrieve from that user's messages.

### API Endpoints (Phase 2)

#### `GET /tags?channel_label=<label>`
Returns combined system + user tags with metadata.

Response:
```json
{
  "system_tags": [
    {"name": "code", "state": "core", "hits": 1285, "scope": "system"},
    ...
  ],
  "user_tags": [
    {"name": "zheng-survey", "state": "core", "hits": 90, "scope": "user"},
    ...
  ]
}
```

#### `GET /tags/system`
Returns system tags only.

#### `GET /tags/user/<label>`
Returns user tags for a specific user.

#### `POST /tags/user/<label>/add`
Add a new user tag. Body:
```json
{
  "name": "zheng-survey",
  "description": "Zheng consulting survey platform",
  "keywords": ["zheng", "lilyzhengfair", "culture assessment"]
}
```
If `keywords` is omitted, the server should infer them from the tag name and return a suggestion for confirmation.

#### `DELETE /tags/user/<label>/<tag_name>`
Archive a user tag (don't delete — move to archived state). Optionally strip from corpus.

#### `POST /tags/user/<label>/retag`
Retag that user's corpus with updated user tags. Only touches messages with matching `channel_label`.

### `/tag` Command (Phase 3 — deferred)

Register as an OpenClaw native command via the contextgraph plugin:

- `/tag` → calls `GET /tags?channel_label=<current_user>`
- `/tag system` → calls `GET /tags/system`
- `/tag user` → calls `GET /tags/user/<current_user>`
- `/tag user add <name>` → interactive: infer keywords, confirm with user, call `POST /tags/user/.../add`
- `/tag user remove <name>` → calls `DELETE /tags/user/.../<name>`

## Migration Plan

### Step 1: Backfill `channel_label` on existing messages

```python
# Based on session_id patterns
UPDATE messages SET channel_label = 'rich' 
  WHERE session_id LIKE '%agent:main%' AND channel_label IS NULL;
UPDATE messages SET channel_label = 'dana'
  WHERE session_id LIKE '%glados-dana%' AND channel_label IS NULL;
UPDATE messages SET channel_label = 'cron'
  WHERE session_id LIKE '%cron:%' AND channel_label IS NULL;
```

### Step 2: Move Rich-specific tags from tags.yaml to user file

Move these from `tags.yaml` to `~/.tag-context/tags.user/rich.yaml`:
- `zheng-survey`
- `skill-registry`
- `project-status`
- `family` (has Rich-specific names: dana, terry, lynae, lily, ivy)
- `yapCAD`
- `shopping-list` (Rich's shopping bot)
- `voice-pwa` (Rich's voice PWA)
- `openclaw` (Rich-specific instance)

Keep as system tags:
- `code`, `ai`, `llm`, `devops`, `security`, `networking`
- `recommendation`, `travel`, `weather`, `health`, `household`
- `calendar`, `email`, `food`, `location`
- `reinforcement-learning`, `space`, `robotics`
- `context-management`, `monitoring`, `agents`

### Step 3: Update engine.ts to pass channel_label

### Step 4: Deploy and verify

## Implementation Tasks

### Task Group A: Data Layer (store.py, backfill)
1. Add `channel_label` filter to `get_by_tag()` and `get_recent()`
2. Backfill `channel_label` on existing messages
3. Unit tests for filtered queries

### Task Group B: Tagger Layer (fixed_tagger.py, tags.yaml split)
1. Refactor `FixedTagger` to accept user tag path
2. Create `~/.tag-context/tags.user/rich.yaml` with Rich-specific tags
3. Remove Rich-specific tags from `tags.yaml`
4. Unit tests for merged tag loading

### Task Group C: Assembly Layer (assembler.py)
1. Pass `channel_label` through assembly pipeline
2. Use channel-filtered `get_by_tag()` for user tags
3. Unit tests for scoped assembly

### Task Group D: API Layer (server.py, engine.ts)
1. Add `channel_label` parameter to `/tag`, `/assemble`, `/compare`
2. New endpoints: `/tags`, `/tags/system`, `/tags/user/<label>`, `/tags/user/<label>/add`, `/tags/user/<label>/retag`
3. Update `engine.ts` to pass `channel_label` during ingest and afterTurn
4. Integration tests

### Task Group E: Registry Layer
1. Per-user registry files at `~/.tag-context/tags.user.registry/<label>.json`
2. Ensemble merges system + user active tags
3. Unit tests

## Agent Execution Plan

1. **Coordinator (claude-sonnet-4-6)** — reads this spec, breaks into task groups, generates detailed prompts for coding agents
2. **Coding agents (ollama/qwen2.5-coder:32b)** — generate code edits for each task group
3. **Reviewer (claude-opus-4-6)** — reviews all generated code for correctness, test coverage, edge cases

Task groups A-E can be partially parallelized:
- A and B are independent
- C depends on A (channel_label filter)
- D depends on A + B + C
- E depends on B

Suggested execution order: A + B in parallel → C → E → D

## Test Strategy

Each task group includes unit tests. Integration test at the end:
1. Backfill Rich's messages
2. Add a user tag for Rich
3. Ingest a new message with channel_label
4. Assemble context — verify user-tagged messages appear, other users' don't
5. Remove the user tag — verify retrieval reverts

## Success Criteria

- [ ] System tags work exactly as today for all users
- [ ] User tags only surface in that user's context assembly
- [ ] `/tags` API endpoints return correct scoped results
- [ ] New user tags can be added via API without service restart
- [ ] Retagging scopes to user's messages only
- [ ] No regression in existing assembler tests
