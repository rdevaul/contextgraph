# Channel Labels & Per-Agent Memory Assembly

## Overview

Channel labels provide memory isolation between different users/contexts in the
contextgraph system. Each ingested turn can carry a `channel_label` that identifies
which communication channel it originated from. During memory assembly, turns are
filtered based on the requesting agent's access rules.

## Label Taxonomy

| Label | Description |
|-------|-------------|
| `rich-dm` | Rich's direct messages (private 1:1 with GLaDOS) |
| `rich-household` | Household shared channel (visible to all household agents) |
| `dana-dm` | Dana's direct messages |
| `terry-dm` | Terry's direct messages |
| `lily-dm` | Lily's direct messages |
| `lynae-dm` | Lynae's direct messages |

## Agent Access Rules

Each agent can only see turns from channels it has access to. Unlabeled turns
(legacy data without `channel_label`) are excluded from filtered queries.

| Agent ID | Accessible Channels |
|----------|-------------------|
| `main` | `rich-dm`, `rich-household` |
| `glados-rich` | `rich-dm`, `rich-household` |
| `glados-household` | `rich-household` |
| `glados-dana` | `dana-dm`, `rich-household` |
| `glados-terry` | `terry-dm`, `rich-household` |
| `glados-lily` | `lily-dm` |
| `glados-lynae` | `lynae-dm` |

## How It Works

### Ingestion

When calling `/ingest`, pass the `channel_label` field:

```json
{
  "session_id": "...",
  "user_text": "...",
  "assistant_text": "...",
  "timestamp": 1711234567.0,
  "channel_label": "rich-dm"
}
```

### Memory Assembly

> **Note:** The `update_memory_dynamic.py` script has been removed (2026-03-31).
> Per-agent context filtering is now handled by the plugin assembler at query time,
> using channel labels to scope retrieval to the appropriate agent's access list.

### Memory Synthesis

Use `synthesize_memory.py` to combine system-wide and per-agent memory:

```bash
python3 scripts/synthesize_memory.py \
  --system-file /path/to/SYSTEM_MEMORY.md \
  --user-file /path/to/agent-specific-memory.md \
  --output-file /path/to/MEMORY_ACTIVE.md
```

The script checks mtimes and only regenerates when sources have changed.

## Adding New Channels/Agents

1. Add the channel label to the taxonomy above
2. Update `AGENT_CHANNEL_ACCESS` in `scripts/channel_access.py`
3. Add corresponding tests in `tests/test_channel_access.py`
