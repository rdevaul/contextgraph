<!-- HISTORICAL: Mar 2026 code review of user-scoped tags. Retained for reference only. Not actively maintained. -->
# Code Review: User-Scoped Tags v1

**Reviewer:** Opus (automated code review)  
**Date:** 2026-03-27  
**Spec:** `docs/spec-user-tags-v1.md`  
**Diff:** 8 files changed, +800 / -164 lines

---

## Correctness

The implementation correctly follows the spec across all five task groups:

- **Store (A):** `channel_label` column added via migration v5, `get_by_tag()` and `get_recent()` both accept optional `channel_label` filter with correct SQL WHERE clause construction. Backfill logic matches the spec's session_id pattern mappings.
- **Tagger (B):** `FixedTagger` cleanly loads system + user tags and merges with user-override-on-collision semantics. Hot-reload checks both mtimes. The `for_channel()` factory is a nice ergonomic touch.
- **Assembler (C):** Correctly applies `channel_label` filter only to user-scoped tags while leaving system tags unfiltered. The `user_tag_set` membership check in the topic layer loop is correct.
- **Registry (E):** Per-user registries at `~/.tag-context/tags.user.registry/<label>.json` with independent lifecycle. `get_active_tags_for_channel()` merges system + user active tags correctly.
- **API (D):** All spec'd endpoints implemented: `GET /tags`, `GET /tags/system`, `GET /tags/user/<label>`, `POST /tags/user/<label>/add`, `DELETE /tags/user/<label>/<tag_name>`, `POST /tags/user/<label>/retag`, `POST /admin/backfill-channel-labels`. The `/assemble` endpoint correctly loads user tag names to pass to the assembler.

**Tag migration from `tags.yaml` to `rich.yaml`:** All 8 Rich-specific tags (`zheng-survey`, `skill-registry`, `project-status`, `family`, `yapCAD`, `shopping-list`, `voice-pwa`, `openclaw`) moved correctly. System tags retained. The user file has `version: 1` and proper YAML structure.

## Security

### 🔴 CRITICAL: Path Traversal in User Tag/Registry File Loading

**No input validation on `label` parameter in API endpoints.** An attacker could send:

```
GET /tags/user/../../etc/passwd
POST /tags/user/../../../tmp/evil/add
```

This would resolve to paths outside the intended `~/.tag-context/tags.user/` directory.

**Affected locations:**
- `api/server.py:295` — `USER_TAGS_DIR / f"{channel_label}.yaml"` in `/assemble`
- `api/server.py:719` — `USER_TAGS_DIR / f"{label}.yaml"` in `POST /tags/user/{label}/add`
- `api/server.py` — all `/tags/user/{label}/*` endpoints
- `fixed_tagger.py:72` — `USER_TAGS_DIR / f"{channel_label}.yaml"` in `for_channel()`
- `tag_registry.py:226` — `USER_REGISTRY_DIR / f"{channel_label}.json"` in `get_user_registry()`

**Impact:** On a local-only service (127.0.0.1:8350) the blast radius is limited, but the `POST /tags/user/{label}/add` endpoint **writes** YAML files, so a crafted label could write to arbitrary paths with `.yaml` extension.

**Fix:** Validate that label matches `^[a-z0-9][a-z0-9_-]{0,63}$` (or similar) and reject anything with `/`, `..`, or path separators. Applied below as a critical fix.

### Minor Security Notes
- Prompt injection sanitization on ingest is good (`_sanitize_for_storage`)
- The service binds to `127.0.0.1` only — correct for local use
- YAML loading uses `safe_load` — no arbitrary code execution risk

## Completeness

### Implemented (Task Groups A–E):
- ✅ `channel_label` column + migration + backfill
- ✅ `get_by_tag()` / `get_recent()` channel filtering
- ✅ `FixedTagger` user tag loading + merge + hot-reload
- ✅ Tags moved from `tags.yaml` to `~/.tag-context/tags.user/rich.yaml`
- ✅ Scoped assembly in `assembler.py`
- ✅ Per-user registries in `tag_registry.py`
- ✅ All API endpoints from spec
- ✅ Backfill endpoint

### Deferred (as expected per spec):
- ❌ `engine.ts` changes (Phase 3 — passing `channel_label` during ingest/afterTurn)
- ❌ `/tag` CLI command (Phase 3)
- ❌ Backfill migration script (endpoint exists, but no auto-run on deploy)
- ❌ Keyword inference on `POST /tags/user/{label}/add` when keywords omitted (currently just splits the tag name — spec says "infer and return suggestion for confirmation")

### Gaps:
1. **`rich.yaml` missing `version` key at top level** — The file has `version: 1` but `_parse_tag_specs` only reads `data.get("tags", [])`. The `version` field is decorative. Not a bug, but the spec implies versioned format support.
2. **`retag_user_corpus` uses system ensemble, not channel-scoped ensemble** — It uses `FixedTagger.for_channel(label)` alone, bypassing baseline and GP taggers. This means retagging only applies fixed-rule tags, not the full ensemble. May be intentional (user tags are keyword-based) but worth documenting.

## Test Coverage

### What's tested:
- `test_store.py` (13 tests) — covers basic CRUD, tags, external_id, is_automated, summary, migrations, idempotency. **But no tests for `channel_label` filtering or `backfill_channel_labels()`.**
- `test_fixed_tagger.py` (10 tests) — covers basic tagger. **No tests for user tag loading, merge, or `for_channel()`.**
- `test_channel_access.py` (19 tests) — covers the `channel_access.py` script (pre-existing), not the new store/tagger/assembler channel_label paths.
- `test_tag_registry.py` (15 tests) — covers system registry lifecycle. **No tests for `get_user_registry()` or `get_active_tags_for_channel()`.**
- `test_assembler.py` (12 tests) — covers core assembly. **No tests for `channel_label`/`user_tags` parameters.**

### 🔴 Missing test coverage (significant):
1. **`store.get_by_tag(channel_label=...)` filtering** — no unit test verifying that channel_label actually filters
2. **`store.get_recent(channel_label=...)` filtering** — no unit test
3. **`store.backfill_channel_labels()`** — no unit test
4. **`FixedTagger.for_channel()`** — no unit test for user tag merge
5. **`ContextAssembler.assemble(channel_label=..., user_tags=...)`** — no unit test for scoped assembly
6. **`get_user_registry()`** — no unit test
7. **API endpoints** — no integration tests for `/tags`, `/tags/user/*`, `/admin/backfill-channel-labels`, `/tags/user/{label}/retag`
8. **Path traversal** — no test confirming malicious labels are rejected

The git diff shows only `test_tagger_fixes.py` was modified (updating the disabled `has-url` test). **No new tests were added for any of the user-scoped tag functionality.** This is a significant gap.

## Code Quality

### Good:
- Clean separation of concerns across store/tagger/assembler/registry layers
- Hot-reload mechanism in `FixedTagger` is well-implemented
- `for_channel()` factory pattern is ergonomic
- Backfill logic is idempotent (only updates NULL rows)
- Migration system is robust with idempotent column additions

### Issues:

- **`retag_user_corpus` accesses `store._lock` and `store._conn()` directly** (`server.py:830-835`) — this breaks encapsulation. Should add a `store.replace_tags(message_id, new_tags)` method instead.
- **`_parse_tag_specs` imported at call site** in `server.py:297` — should be imported at top of file
- **`import yaml` inside endpoint functions** (`server.py:643, 720`) — should be at module level
- **`get_user_registry()` never returns None** despite the `Optional` return type — it always creates a registry. The None check in `get_active_tags_for_channel()` is dead code.
- **User registry cache (`_user_registry_cache`) is module-level mutable global** — fine for single-process but could cause issues if tests don't clean up (there's a `clear_user_registry_cache()` but tests don't use it)

## Specific Issues

| # | Severity | File:Line | Issue |
|---|----------|-----------|-------|
| 1 | 🔴 Critical | `api/server.py:*`, `fixed_tagger.py:72`, `tag_registry.py:226` | **Path traversal**: No validation on `label`/`channel_label` used in file paths. Malicious input like `../../etc/foo` could read/write outside intended directories. |
| 2 | 🟡 Medium | `tests/*` | **No new tests** for any user-scoped tag functionality (store filtering, tagger merge, scoped assembly, registry, API endpoints). |
| 3 | 🟡 Medium | `api/server.py:830-835` | `retag_user_corpus` accesses `store._lock` and `store._conn()` directly, bypassing the store's public API. |
| 4 | 🟢 Low | `api/server.py:643,720` | `import yaml` inside endpoint functions rather than at module top. |
| 5 | 🟢 Low | `tag_registry.py:218-236` | `get_user_registry()` never returns `None` despite `Optional` return type — misleading signature. |
| 6 | 🟢 Low | `rich.yaml` | No `version` key at top level (it's present but `_parse_tag_specs` ignores it — fine, just decorative). |
| 7 | 🟢 Low | `store.py:backfill_channel_labels()` | Catch-all `SET channel_label = 'rich' WHERE channel_label IS NULL` could mislabel messages from future users added before backfill. Safe now since Rich is the only user, but fragile. |

## Verdict

### **Ship with fixes** 🟡

The architecture is solid and the implementation correctly follows the spec across all layers. The one critical issue (path traversal) must be fixed before shipping to prevent arbitrary file writes via crafted label parameters. Test coverage for the new functionality is missing entirely — this should be addressed soon but doesn't block a deploy to the single-user local service.

**Before ship:**
1. Fix #1 (path traversal validation) — **applied below**

**Soon after ship:**
2. Add unit tests for store channel_label filtering, tagger merge, scoped assembly
3. Add integration tests for new API endpoints
4. Refactor `retag_user_corpus` to use a proper store method

---

*Review generated by Opus subagent, 2026-03-27*
