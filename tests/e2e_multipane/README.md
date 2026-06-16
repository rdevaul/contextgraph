# E2E Multipane Harness

End-to-end validation of ContextGraph running in the **multipane** environment
with the **Current Thing (`/thing`) goals system active**. Unlike the unit-level
`tests/pane-isolation/` suite (which drives the assembler against a temp DB),
this harness hits the **live running server over HTTP** and exercises the real
`/ingest` → `/assemble` → `/current-thing` paths.

It exists to catch the two failure modes Garrett + Rich keep hitting:

1. **Cross-channel / cross-pane bleed** — context from one user's pane leaking
   into another's assembled context (the stale-plugin regression fixed
   2026-06-16).
2. **Current Thing pollution / drift** — the `/thing` goal block injecting the
   wrong identity, exceeding its token budget, or leaking one pane's goal into
   another pane.

## Running

Server must be up on port 8302 (it normally is, via LaunchAgent).

```bash
cd ~/Projects/contextgraph
python3 tests/e2e_multipane/run_e2e.py                 # all groups
python3 tests/e2e_multipane/run_e2e.py --only isolation
python3 tests/e2e_multipane/run_e2e.py --only current_thing
python3 tests/e2e_multipane/run_e2e.py --keep          # leave seeded rows for debug
python3 tests/e2e_multipane/run_e2e.py --base-url http://localhost:8302
```

Stdlib-only, no pytest dependency. Exit code 0 = all passed.

## Self-cleaning

Every run seeds rows whose `id` is prefixed with a unique `run_tag`
(`e2e-<epoch>-<rand>`). On exit the harness deletes exactly those rows directly
against `~/.tag-context/store.db` (the API has no delete-by-tag endpoint). It
never touches other rows. Verified: corpus row-count delta = 0 across a run.
Use `--keep` to retain rows for inspection.

## What each check proves

### Group A — isolation
| Check | Proves |
|---|---|
| A1 | `scope=session` garrett pane does not return a rich-labeled secret |
| A2 | `scope=user` rich query does not return a garrett-labeled secret |
| A3 | `scope=session` excludes a sibling pane on the **same** channel |
| A4 | `scope=global` escape hatch still returns cross-session rows (not over-filtered) |
| A5 | server echoes the `effective_scope` / `effective_channel_label` it applied |
| A6 | a `:dashboard:` session_id sent with `scope=user` is coerced to `session` (legacy safety net for old plugin builds) |

### Group B — Current Thing (`/thing`)
| Check | Proves |
|---|---|
| B1 | block injected when `inject_current_thing=true` and feature flag on |
| B2 | block names the correct user/namespace for the pane |
| B3 | `current_thing_tokens` ≤ 400 (budget cap honored) |
| B4 | `inject_current_thing=false` suppresses the block |
| B5 | a user-pinned `goals.primary` (locked) survives a later assemble |
| B6 | one pane's pinned goal does NOT appear in another pane's context or block |

If `CONTEXT_CURRENT_THING_ENABLED=0`, B1 fails and B2/B3 self-skip with a note —
that's the signal the feature flag is off, not a real regression.

## Relationship to other tests

- `tests/pane-isolation/` — unit-level, temp DB, no server. Run both.
- `tests/test_e2e_smoke.py` — pytest, pins/tool-state pipeline. Complementary.
- This harness is the only one that validates the `/thing` system live.
