# Changelog

All notable changes to the contextgraph project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v1.0-rc1] - 2026-03-22

**Release candidate 1** — Memory integration live, automated turn filtering, lazy summarization, and production dashboard. Context Graph is production-ready with 11.8% token efficiency gains over linear retrieval.

### Major Features

#### Memory Integration (Phase 4 Complete)
- **Live MEMORY.md integration** — `update_memory_dynamic.py` runs every 4 hours via launchd (`com.contextgraph.update-memory`), writing graph-assembled context directly to `~/.openclaw/workspace/MEMORY.md`
- **HTML marker-based section replacement** — Dynamic Context section is updated without touching curated long-term memory above it
- **Dual-layer context assembly** — Agents receive both persistent context (MEMORY.md Dynamic Context section, updated every 4h) and live per-turn retrieval
- **`--live` flag enforcement** — Production mode requires explicit opt-in; `--shadow` mode writes to `SHADOWMEMORY.md` for validation

#### Automated Turn Filtering
- **Cron/heartbeat/subagent filtering** — Non-retrieval turns automatically excluded from quality metrics and dashboard stats
- **Retrieval-only evaluation** — `/quality` endpoint reports `retrieval_turns_evaluated` instead of all turns, providing accurate quality scores
- **Cleaner metrics** — Noise from scheduled tasks and background operations no longer dilutes quality measurements

#### Lazy Message Summarization
- **35% token budget cap** — Individual messages exceeding 35% of budget are summarized on-the-fly to prevent giant turns from swamping context
- **Configurable model** — Defaults to Claude Haiku; supports local model fallback
- **Preserves semantic content** — Summarization retains key information while preventing token budget domination

#### Production Dashboard
- **Chart.js visualization** — Full-featured dashboard at `http://localhost:8300/dashboard`
- **Token efficiency scatterplot** — Graph vs linear comparison across 580+ retrieval turns
- **Quality metrics** — Context density, reframing rate, cache hit rate (99%+ achieved)
- **Efficiency lead tracking** — Cumulative token savings over time (~11.8% vs linear baseline)
- **Tag distribution** — Most-used tags with counts, sorted by frequency

### Performance & Infrastructure

#### Token Efficiency
- **11.8% token savings** vs linear retrieval baseline (measured over 580+ retrieval turns)
- **99%+ cache hit rate** on context assembly via prompt caching
- **3,423 avg tokens/query** (graph) vs 3,717 (linear) — 294 fewer tokens while delivering more relevant context

#### Database & Concurrency
- **SQLite WAL mode** — Write-Ahead Logging eliminates contention between API server, memory updater, and CLI tools
- **Concurrent access** — Multiple processes can safely read/write without blocking

#### Quality Monitoring
- **`/quality` endpoint** — Returns zero-return rate, tag entropy, alert status, and top tags
- **Retrieval-aware metrics** — Only evaluates actual retrieval turns (excludes cron/heartbeat/subagent)
- **Alert thresholds** — Automatic alerts when zero-return rate >25% or tag entropy <2.0

### Documentation Updates
- **README.md** — Updated status to v1.0-rc1, added Operations section with launchd service details, updated roadmap to reflect Phase 4 completion
- **docs/MEMORY_INTEGRATION.md** — Rewritten to reflect live status, added service management instructions, removed "shadow mode" references
- **Architecture diagram** — Updated to show automated turn filtering, lazy summarization, and IDF tag filtering

### Services & Operations
- **API server** (`com.contextgraph.api`) — Port 8300, logs to `/tmp/tag-context.log`
- **Memory updater** (`com.contextgraph.update-memory`) — Runs every 4 hours, logs to `/tmp/update_memory_dynamic.log`
- **Dashboard** — http://localhost:8300/dashboard
- **Health checks** — `curl http://localhost:8300/health` (service alive), `curl http://localhost:8300/quality` (retrieval working)

### Bug Fixes
- **WAL contention** — Fixed SQLite database locking issues with concurrent access
- **Envelope stripping** — Channel metadata no longer indexed as user text
- **IDF tag filtering** — Over-generic tags automatically down-weighted to maintain topic discrimination

---

## [Unreleased]

### Planned for Phase 5
- **Graph-primary mode** — Graph becomes default context engine, linear window available as fallback via `/graph off`
- **Extended validation** — Monitor production metrics over extended period before promoting to default

---

## Version History

- **v1.0-rc1** (2026-03-22) — Memory integration live, turn filtering, lazy summarization, dashboard
- **Phase 3** (2026-03) — Native OpenClaw plugin, `/graph on|off` toggle, comparison logging
- **Phase 2** (2026-03) — Shadow mode validation, 11.8% token efficiency vs linear baseline
- **Phase 1** (2026-02) — Passive collection, GP tagger evolution, 812+ interaction corpus

---

[v1.0-rc1]: https://github.com/rarebreed/tag-context/releases/tag/v1.0-rc1
