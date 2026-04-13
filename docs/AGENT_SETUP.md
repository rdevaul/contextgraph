<!-- HISTORICAL: This document is preserved from an earlier era. Many details are stale (references to ensemble.py, old component counts). See README.md and docs/SPEC.md for current system documentation. Retained for reference only. Not actively maintained. -->
# AGENT_SETUP.md — Operational Guide for OpenClaw Agents

This document describes the current deployment of the Context Graph system
and how to set it up, maintain it, or recover it from scratch. Written for
an OpenClaw agent taking over maintenance.

## What This System Does

The Context Graph replaces OpenClaw's default linear sliding window with
tag-based context retrieval. Every user/assistant turn is stored and tagged.
When a new message arrives, the plugin queries the graph API to assemble
topically-relevant context rather than just the most recent N messages.

Additional features:
- **Sticky threads** — tool chains (multi-step exec work) are pinned so
  context isn't broken mid-task by compaction.

## Repository

```
https://github.com/rdevaul/contextgraph
Local: ~/Projects/tag-context
```

## Current Status (as of 2026-04-08)

- **Phase 3 complete** — native plugin live, `/graph on|off` working
- **All tests passing** (`python3 -m pytest tests/ -v`)
- **v1.1 fixes applied** — envelope stripping, IDF tag filtering, `/quality` endpoint

---

## Architecture Overview

```
OpenClaw (Telegram/Voice/etc.)
        │
        ▼ (every turn)
  plugin/index.ts          ← OpenClaw native plugin
        │
        ├── POST /ingest    ← store turn in SQLite + tag it
        ├── POST /assemble  ← retrieve relevant context for incoming msg
        ├── POST /sticky    ← manage tool-chain pins
        └── GET  /compare   ← graph vs linear stats (comparison logging)
        │
        ▼
  api/server.py             ← FastAPI on port 8300
        │
        ├── store.py        ← SQLite MessageStore + tag index
        ├── assembler.py    ← recency + topic context layers
        ├── tagger.py       ← rule-based tagger (v0)
        ├── gp_tagger.py    ← GP-evolved tagger
        └── ensemble.py     ← weighted mixture model
```

Data lives in:
```
data/messages.db         ← SQLite (all turns + tags)
data/harvester-state.json ← last harvest timestamp
data/tag_registry.json   ← NOT tracked in git (live runtime file)
~/.tag-context/comparison-log.jsonl  ← per-turn graph vs linear stats
```

---

## Services (macOS launchd)

### 1. Context Graph API — `com.glados.tag-context`

The Python FastAPI server.

```bash
# Status (PID present = running; just exit code = crashed)
launchctl list | grep tag-context

# Start / stop
launchctl start com.glados.tag-context
launchctl stop com.glados.tag-context

# Restart after code changes (must unload+load to re-read plist)
launchctl unload ~/Library/LaunchAgents/com.glados.tag-context.plist
launchctl load ~/Library/LaunchAgents/com.glados.tag-context.plist

# Logs
tail -f /tmp/tag-context.log

# Health check
curl http://localhost:8300/health
# → {"status":"ok","messages_in_store":...,"engine":"contextgraph"}
```

**Plist:** `~/Library/LaunchAgents/com.glados.tag-context.plist`
**Python:** `~/Projects/tag-context/venv/bin/python3`
**Port:** 8300
**Log:** `/tmp/tag-context.log`

> Note: There is also a `com.contextgraph.api` service from the template-based
> installer. The active service is `com.glados.tag-context`. Don't run both —
> they both bind port 8300 and will crash-loop each other.

### 2. OpenClaw Plugin

The plugin (`plugin/index.ts`) runs inside the OpenClaw gateway. After
making changes to the plugin:

```bash
cp plugin/index.ts ~/.openclaw/extensions/contextgraph/index.ts
openclaw gateway restart
```

Toggle graph mode in chat:
```
/graph on     # enable context graph
/graph off    # fall back to linear window
/graph        # show current status + API health
```

---

## First-Time Setup (new machine)

```bash
cd ~/Projects/tag-context
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm   # optional but recommended
```

Install the launchd service:
```bash
./scripts/install-service.sh
# or with explicit Python:
./scripts/install-service.sh --python ~/Projects/tag-context/venv/bin/python3
```

This reads `service/com.contextgraph.api.plist.template`, substitutes paths,
writes to `~/Library/LaunchAgents/`, and loads it.

Install the OpenClaw plugin:

> ⚠️ **Check first:** Run `openclaw plugins list | grep contextgraph` before installing.
> If you see `loaded` — the plugin is already installed via auto-load. Do NOT copy files
> again or add it to `openclaw.json`. Duplicate registration crashes the gateway.
> See `plugin/README.md` for the full safe installation procedure.

```bash
# Only if NOT already listed by `openclaw plugins list`:
mkdir -p ~/.openclaw/extensions/contextgraph
cp plugin/index.ts ~/.openclaw/extensions/contextgraph/
openclaw gateway reload   # use reload, not restart — restart kills active sessions
```

---

## Maintenance Scripts

### Harvester (collect new turns from session logs)

```bash
cd ~/Projects/tag-context
source venv/bin/activate
python3 scripts/harvester.py
# or with options:
python3 scripts/harvester.py --since 2026-03-01 --verbose --dry-run
```

Reads `~/.openclaw/agents/main/sessions/` JSONL files. Tracks state in
`data/harvester-state.json` — only processes turns newer than last run.

Skips cron/hook/group sessions (too noisy). Harvests:
- `agent:main:main` — primary DM session
- `agent:main:telegram:*` — Telegram DMs
- `agent:main:voice*` — Voice PWA sessions

### GP Tagger Evolution (periodic retraining)

```bash
cd ~/Projects/tag-context
source venv/bin/activate
python3 scripts/evolve.py --generations 50
```

Retrains the genetic-programming tagger using stored interactions.
Run when the corpus has grown substantially (e.g., every few weeks).
Output: `data/gp-tagger.pkl` (gitignored).

### Replay (retag entire corpus with new tagger)

```bash
python3 scripts/replay.py
```

Applies current ensemble tagger to all stored messages. Run after
evolving a new GP tagger.

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `api/server.py` | FastAPI server — all endpoints |
| `plugin/index.ts` | OpenClaw context engine plugin |
| `store.py` | SQLite MessageStore + tag index |
| `assembler.py` | Context assembly (recency + topic layers) |
| `tagger.py` | Rule-based baseline tagger |
| `gp_tagger.py` | Genetically-evolved tagger |
| `ensemble.py` | Weighted mixture model |
| `sticky.py` | Sticky thread pin logic |
| `scripts/harvester.py` | Collect turns from OpenClaw session logs |
| `scripts/evolve.py` | Retrain GP tagger |
| `scripts/replay.py` | Retag full corpus |
| `utils/text.py` | `strip_envelope()` — strips channel metadata before indexing/querying |
| `scripts/install-service.sh` | launchd service installer |
| `service/*.plist.template` | launchd plist templates (path-substituted) |
| `data/messages.db` | SQLite DB (gitignored) |
| `data/tag_registry.json` | Live tag registry (gitignored — runtime state) |

---

## Diagnostics

### API not responding

```bash
# Check if service is running (needs a PID, not just exit code)
launchctl list | grep tag-context

# If crashed, check logs
tail -50 /tmp/tag-context.log

# Restart
launchctl stop com.glados.tag-context
launchctl start com.glados.tag-context

# If env vars need updating, must unload+load (not stop+start)
launchctl unload ~/Library/LaunchAgents/com.glados.tag-context.plist
launchctl load ~/Library/LaunchAgents/com.glados.tag-context.plist
```

### Plugin not calling API

```bash
# Check OpenClaw plugin is installed
ls ~/.openclaw/extensions/contextgraph/

# Check graph is enabled
# (send /graph in chat to see status)

# Restart gateway after plugin changes
openclaw gateway restart
```

### Retrieval quality check

Before concluding retrieval is healthy, always check `/quality` — not just `/health`:

```bash
curl http://localhost:8300/quality | python3 -m json.tool
```

Key fields:
- `zero_return_rate` — fraction of recent turns returning 0 graph messages. >0.25 = alert.
- `tag_entropy` — how evenly tags are distributed. <2.0 = over-generic tags, topic layer degraded.
- `alert` / `alert_reasons` — automated summary.

### Context quality degraded / assembler returning noise

Likely causes:
1. **Envelope pollution in old data** — if corpus was ingested before v1.1, stored
   `user_text` may contain OpenClaw channel metadata. Re-ingest affected records or
   run `scripts/replay.py` to retag (stripping is now applied at ingest time in v1.1+).
2. **Over-generic tags** — IDF filtering (>30% corpus frequency = skip) is applied
   automatically. If all tags are high-frequency, the fallback uses the lowest-frequency
   half. Check `zero_return_rate` via `/quality`.
3. **New cron/heartbeat turns in DB** — harvester may have ingested noisy sessions.
4. **Oversized records blocking assembler budget** — check if any record has token
   count > 5000.
5. **tag_registry diverged** — run `python3 scripts/replay.py` to retag.

### Comparison log growing too large

```bash
# Check size
wc -l ~/.tag-context/comparison-log.jsonl

# Rotate (keep last 1000 lines)
tail -1000 ~/.tag-context/comparison-log.jsonl > /tmp/cl.tmp && mv /tmp/cl.tmp ~/.tag-context/comparison-log.jsonl
```

---

## Tests

```bash
cd ~/Projects/tag-context
source venv/bin/activate
python3 -m pytest tests/ -v
```

Key test files:
- `tests/test_sticky_e2e.py` — end-to-end sticky thread lifecycle
- `tests/test_sticky_server_detection.py` — gateway restart recovery
- `tests/test_assembler.py` — context assembly logic
- `tests/test_compare_sticky.py` — comparison logging

---

## Transition Roadmap

- [x] Phase 1 — Passive collection (corpus: 800+ interactions)
- [x] Phase 2 — Shadow evaluation (graph beats linear baseline)
- [x] Phase 3 — Native plugin live (`/graph on|off`, sticky threads)
- [ ] Phase 4 — Graph-primary (default on, linear as fallback)

---

## Notes for Agents

### ⚠️ Gateway Restart Footgun

Do NOT use `openclaw gateway stop` / `openclaw gateway restart` to reload the
context graph plugin. These commands orphan the LaunchAgent, kill all active
sessions (Discord, Telegram, Voice), and disconnect the gateway entirely.

**Correct way to reload the plugin after a change:**

```bash
# Copy updated plugin
cp plugin/index.ts ~/.openclaw/extensions/contextgraph/index.ts

# Graceful reload (SIGUSR1 — keeps connections alive)
openclaw gateway reload
# or via config API:
curl -X POST http://localhost:3001/api/gateway/reload
```

If you're not sure which method is available in your OpenClaw version, use
`openclaw gateway --help` and check for a `reload` or `signal` subcommand.
Only use `stop`/`restart` as a last resort when the gateway is already dead.

---

- **Never run the API manually** while the launchd service is active —
  port 8300 conflict will crash-loop both.
- **`launchctl stop/start` does NOT re-read the plist** — always use
  `unload`/`load` after changing environment variables or the plist itself.
- **`data/tag_registry.json` is gitignored** — it's a live runtime file
  that accumulates real conversation tags. Don't try to commit it.
- The `com.glados.tag-context` label is local convention. The template
  installer uses `com.contextgraph.api`. Both are fine — just don't run both.
