# cgquery — ContextGraph forensic query & analysis tool

A command-line tool to interactively inspect the context graph: browse by
**user/channel**, **subchannel**, **session**, and **topic (tag)**; count
matching records; and dump the **assembled context window** that would be
produced for a query — via **direct DB query** *and* via the **live `/assemble`
API**, so the two can be cross-checked.

It is **read-only** on the corpus (`sqlite3 mode=ro`). It never mutates anything.

Location: `~/Projects/contextgraph/cgquery.py`
DB: `~/.tag-context/store.db` · API: `http://localhost:8302`

---

## Quick start

```bash
cd ~/Projects/contextgraph

# See what's in the graph: channels, subchannels, top tags, totals
python3 cgquery.py facets

# Count records matching a structured query
python3 cgquery.py count --channel garrett --subchannel fea
python3 cgquery.py count --tag yapCAD --channel rich

# Browse rows (most recent first); add --show-text for content
python3 cgquery.py browse --channel rich --tag debugging --limit 10 --show-text

# Dump the assembled context window (topic + sticky, NO recency floor),
# reproduced from the DB AND fetched from the API, with a cross-check + leak scan
python3 cgquery.py assemble --query "where are we on the FEA work" \
        --channel garrett --scope user --check-api
```

---

## Commands

### `facets`
Lists every channel, the top 30 subchannels, the top 30 tags, and the total
record count. Start here.

### `count [--channel C] [--subchannel S] [--session SID] [--tag T] [--include-automated]`
Prints the number of records matching the structured filter (AND semantics).
Use `--channel '<NULL>'` / `--subchannel '<NULL>'` to match unlabeled rows.

### `browse <same filters> [--limit N] [--show-text]`
Lists matching rows, most-recent first, with timestamps, channel/subchannel,
id, and tags. `--show-text` adds shortened user/assistant text.

### `assemble --query "..." [scoping] [--check-api] [--api-only|--db-only]`
The core forensic command. Reproduces the context window for an incoming query.

**Scoping flags:** `--channel`, `--subchannel`, `--session`, `--scope`.

**Scopes** (mirror `assembler.assemble`):
| scope | isolation |
|-------|-----------|
| `global` | none — retrieves across ALL users (the old leaky default) |
| `user` | filter topic + sticky by `channel_label` → **cross-user isolation** |
| `session` | filter by `session_id` → cross-session isolation |
| `subchannel` | per-pane recency; topic narrows to channel |

**What it does:**
- Infers tags via the API's `/tag` endpoint (so both paths use the *same* tags),
  unless you pass `--tags a,b,c`.
- Calls the live `/assemble` API with `recency_floor=0` and
  `inject_current_thing=false` (forensic mode — strips the request-time-relative
  layers so you see only the durable topic/sticky content).
- Reproduces the **topic layer** directly from the DB for the same scope.
- With `--check-api`: diffs the two result sets and **scans for cross-channel
  leaks** — any API row whose `channel_label` ≠ the requested `--channel`
  (under `user`/`subchannel` scope) is flagged and the tool exits **2**.

**Modes:** `--api-only` (just the API), `--db-only` (just DB reproduction).

---

## Reading the cross-check

```
=== CROSS-CHECK (API vs DB-direct) ===
  API rows:       5
  DB-direct rows: 8
  intersection:   4
  only in API (2): [...]      # sticky/pins + relevance fill the API adds
  only in DB  (4): [...]      # topic candidates the API's budget/ranking dropped
  ✓ no cross-channel leak (all API rows match channel=garrett or NULL)
```

Divergence between API and DB-direct is **expected** — DB-direct reproduces only
the topic layer, while the API also injects sticky/pins and does relevance-ranked
budget fill. The number that matters for the contamination investigation is the
**leak line**:

- `✓ no cross-channel leak` — the firewall held for this query.
- `⚠ CROSS-CHANNEL LEAK: N API rows from other channels` (exit 2) — contamination.

---

## Demonstrating the contamination bug

```bash
# CLEAN: scope=user correctly isolates garrett from rich
python3 cgquery.py assemble --query "yapCAD DSL and multigraph" \
        --channel garrett --scope user --check-api          # → ✓ clean, exit 0

# LEAK: scope=global (what an old plugin build sends when it omits scope)
# pulls rich rows into a garrett query
python3 cgquery.py assemble --query "yapCAD DSL and multigraph" \
        --channel garrett --scope global --api-only         # → rich rows appear
```

**Conclusion the tool makes visible:** the cross-user firewall works *only when
`scope=user`/`subchannel` is actually sent on the `/assemble` call*. Any caller
that omits scope falls back to `global` and leaks. Use this to audit whether the
deployed plugin is sending scope correctly.

---

## Notes / limitations

- DB-direct sees only the **topic layer**. Sticky/pins live in the API process's
  in-memory pin manager and are not in the DB, so they show up only on the API
  side (this is by design and is called out in the cross-check note).
- The recency-floor layer is deliberately omitted (forensic inspections are not
  tied to a live conversation, so "the immediately prior turn" doesn't exist).
- Exit codes: `0` ok · `2` cross-channel leak detected · non-zero on API errors.
