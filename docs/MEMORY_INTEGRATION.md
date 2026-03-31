# MEMORY_INTEGRATION.md — Context Graph Memory Integration

## Status: DEPRECATED

The `update_memory_dynamic.py` script and associated launchd services
(`com.glados.update-memory`, `com.glados.update-memory-dynamic`) have been
**removed** as of 2026-03-31.

## Why

The script injected raw conversation snippets into `MEMORY.md` every 4 hours.
This approach was:

1. **Redundant** — the Context Graph plugin already provides per-turn topical
   retrieval via the assembler, making static MEMORY.md injection unnecessary
2. **Harmful** — it bloated MEMORY.md with ~20KB of unfiltered content including
   security headers, medical data, and debug logs, causing bootstrap truncation
3. **Stale** — 4-hour refresh meant context was always behind the live plugin

## Current Architecture

Context flows to the agent via two independent paths:

| Path | Mechanism | Freshness |
|------|-----------|-----------|
| **MEMORY.md** | Curated long-term facts, manually maintained | Updated by agent as needed |
| **Context Graph** | Per-turn topical retrieval via plugin assembler | Real-time (every turn) |

No bridge script is needed. The plugin handles retrieval directly.
