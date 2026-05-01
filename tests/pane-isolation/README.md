# Pane Isolation Tests

Tests for the per-pane / per-user retrieval isolation fix shipped in the two
commits referenced by:

- Bus thread: `20260501213940-5b002851`
- Approval:   `20260501220916-a4feb6f0`
- Handoff:    `~/.sybilclaw/workspace-jarvis/projects/contextgraph-pane-isolation/HANDOFF-2026-05-01-rich.md`
- Forensic:   `~/.sybilclaw/workspace-jarvis/projects/multigraph/forensics/agentic-1-assembly-FORENSICS-2026-05-01.md`

## Why these are separate from `tests/`

The main test suite uses the global `pin_manager` singleton in `api/server.py`
and pollutes itself across runs (test_basic_assembly_returns_messages on `main`
already fails with leftover sticky pins). These pane-isolation tests construct
a fresh `MessageStore` per test against a temp SQLite DB, call the assembler
directly, and exercise scope semantics deterministically.

## Running

From `~/Projects/contextgraph`:

```bash
python3 -m pytest tests/pane-isolation/ -v
```

Or run any single file directly:

```bash
python3 tests/pane-isolation/test_part_a_user_scope.py
python3 tests/pane-isolation/test_part_a_cross_user.py
python3 tests/pane-isolation/test_part_b_session_and_global.py
```

## What each test asserts

| Test | What it proves |
|---|---|
| `test_part_a_user_scope.py` | Pane A asking with a shared tag does NOT pull pane B's rows after Part A. Captures before/after retrieval counts. |
| `test_part_a_cross_user.py` | A `channel_label='rich'` row does not bleed into a `channel_label='garrett'` query. Mirrors the 686-row production scenario. |
| `test_part_b_session_and_global.py` | `scope='session'` filters to a single session_id; `scope='global'` returns cross-session results (escape hatch intact). |

## Note on 686-row migration

686/686 `channel_label='garrett'` rows in production already have a
non-null `session_id`. No migration needed for these tests OR for production
to benefit from `scope='session'` retrieval.
