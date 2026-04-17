# Changelog

All notable changes to the contextgraph project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v1.0-rc2] - 2026-03-30

**Release candidate 2** — Dashboard accuracy and usability improvements. Tag frequency now sourced from live registry. Token overhead analysis now supports time-windowed views with stats and chart in sync.

### Dashboard Fixes

#### Time Window Selector
- **Window selector** — All Time / Last 7 Days / Last 24 Hours dropdown added to Token Overhead Analysis section
- **Unified filtering** — changing the window updates both the stats cards (token savings, overhead percentages) and the time series chart together; they always reflect the same dataset
- **Backend filtering** — `/comparison-stats?since=1d|7d` filters the `time_series` payload server-side; windowed requests return all filtered entries (up to 500 for all-time)
- Previously the chart always showed the same 50 most-recent points regardless of window — fixed

#### Tag Frequency Chart
- **Live registry data** — "Top Tags by Frequency" now reads from `/tags` (live `hits` counts) instead of `/comparison-stats` `tag_frequency` (cumulative comparison log counts that never reset)
- Chart label updated to "Tag Hits (live registry)" for clarity
- Stale dashboard counts (e.g. `trading` showing 422 vs actual 188) no longer appear after hard reload

#### Token Savings Stat Card
- **Renamed** — "Graph Coverage vs Linear" → "Token Savings" (the old name was misleading)
- **Color coded** — green for positive savings, red when graph costs more than linear

---

## [v1.0-rc1] - 2026-03-22

**Release candidate 1** — Memory integration live, automated turn filtering, lazy summarization, and production dashboard. Context Graph is production-ready with 11.8% token efficiency gains over linear retrieval.

### Major Features

#### Memory Integration (Phase 4 Complete)
- **Live MEMORY.md integration** — `update_memory_dynamic.py` runs every 4 hours via launchd (`com.glados.update-memory`), writing graph-assembled context directly to `~/.openclaw/workspace/MEMORY.md`
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
- **Chart.js visualization** — Full-featured dashboard at `http://localhost:8302/dashboard`
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
- **API server** (`com.glados.tag-context`) — Port 8302, logs to `/tmp/tag-context.log`
- **Memory updater** (`com.glados.update-memory`) — Runs every 4 hours, logs to `/tmp/update_memory_dynamic.log`
- **Dashboard** — http://localhost:8302/dashboard
- **Health checks** — `curl http://localhost:8302/health` (service alive), `curl http://localhost:8302/quality` (retrieval working)

### Bug Fixes
- **WAL contention** — Fixed SQLite database locking issues with concurrent access
- **Envelope stripping** — Channel metadata no longer indexed as user text
- **IDF tag filtering** — Over-generic tags automatically down-weighted to maintain topic discrimination

---

## [v1.0-rc2] - 2026-03-27

**Release candidate 2** — User-scoped tag taxonomy, channel-labeled ingestion, degenerate text filtering, and OpenClaw plugin synchronization. Expanded tag ontology with personal assistant categories and project-specific keywords.

### Major Features

#### User-Scoped Tag Taxonomy
- **User-specific tag definitions** — New `tags.user/` YAML directory allows per-user tag customization alongside global `tags.main/` ontology
- **API endpoints for user tags** — `/tags/user/<name>`, `/tags/user/<name>/add`, `/tags/user/<name>/delete` endpoints support runtime tag management
- **Path traversal protection** — User tag API validates filenames to prevent directory traversal attacks
- **Personal assistant taxonomy** — Added comprehensive tag set for personal assistant interactions (tasks, reminders, scheduling, goals, finance, health, etc.)
- **Project-specific keywords** — New tags for active projects: `zheng-survey`, `skill-registry`, `project-status`

#### Channel-Labeled Ingestion
- **`channel_label` field** — Messages can now include a `channel_label` field to scope context assembly per conversation/channel
- **Plugin support** — OpenClaw plugin's `ContextEngine` includes `inferChannelLabel()` to detect channel from conversation metadata
- **Per-channel assembly** — Context retrieval can be scoped to specific channels, preventing cross-channel leakage

#### Degenerate Text Filtering
- **Repetitive text detection** — Ingestion now rejects messages with excessive repetition (>40% duplicate 4-grams)
- **Short message filtering** — Messages under 10 characters automatically rejected
- **Quality protection** — Prevents noise from degrading tag quality and retrieval relevance

### OpenClaw Plugin Updates
- **Full engine sync** — Live plugin files synchronized back into repo (`engine.ts`, `api-client.ts`, `index.ts`)
- **Typed API client** — `api-client.ts` provides typed interface for all Context Graph endpoints with `channel_label` support
- **Plugin registration safety** — Documentation updated to prevent duplicate registration crashes

### Tag System Enhancements
- **Code keyword tags** — Added tags for programming concepts: `code-review`, `debugging`, `refactoring`, `testing`, `documentation`
- **Improved salience weighting** — Rebalanced tag scoring to favor distinctiveness over raw frequency
- **Disabled low-signal tags** — `question` and `has-url` tags disabled due to poor discrimination

### Bug Fixes
- **Dashboard JavaScript parse error** — Fixed syntax error preventing dashboard rendering
- **Message summary substitution** — Literal `[summary ...]` placeholders now replaced with actual summaries
- **Oversized message handling** — Messages exceeding size limits now properly bypass summary substitution
- **Hard global budget cap** — Assembler now enforces strict token budget to prevent overruns
- **Health endpoint performance** — `/health` now capped at 1000 messages to prevent timeout on large databases
- **Store method corrections** — Fixed `_conn()` method usage in `store.count()`
- **Envelope metadata cleaning** — OpenClaw runtime/subagent/timestamp prefixes stripped from stored messages
- **Automated message marking** — Scheduled reminder messages correctly marked as automated
- **Task prefix stripping** — Internal task/subagent prefixes removed from indexed content

### Documentation
- **Plugin registration safety** — Added warning about duplicate registration crashes
- **User-scoped tags** — (To be added in this release)
- **Channel labels** — (To be added in this release)
- **Updated API reference** — (To be added in this release)

---

## [Unreleased]

### Planned for Phase 5
- **Graph-primary mode** — Graph becomes default context engine, linear window available as fallback via `/graph off`
- **Extended validation** — Monitor production metrics over extended period before promoting to default

---

## Version History

- **v1.0-rc2** (2026-03-27) — User-scoped tags, channel labels, degenerate text filtering, plugin sync
- **v1.0-rc1** (2026-03-22) — Memory integration live, turn filtering, lazy summarization, dashboard
- **Phase 3** (2026-03) — Native OpenClaw plugin, `/graph on|off` toggle, comparison logging
- **Phase 2** (2026-03) — Shadow mode validation, 11.8% token efficiency vs linear baseline
- **Phase 1** (2026-02) — Passive collection, GP tagger evolution, 812+ interaction corpus

---

[v1.0-rc2]: https://github.com/rarebreed/tag-context/releases/tag/v1.0-rc2
[v1.0-rc1]: https://github.com/rarebreed/tag-context/releases/tag/v1.0-rc1
