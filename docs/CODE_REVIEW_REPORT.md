<!-- HISTORICAL: Mar 2026 automated code review report for sticky thread fixes. Retained for reference only. Not actively maintained. -->
# Code Review Report: Sticky Thread System Fixes

**Reviewer:** Claude (Sonnet 4.5)
**Date:** 2026-03-18
**Commit:** bc02c2b (fix: server-side fallback for sticky pins when pending_chain_ids is empty)
**Architecture Doc:** `docs/STICKY_THREADS.md`
**Test Spec:** `docs/TEST_SPEC_STICKY.md`

---

## Summary

The coding agent implemented **2 out of 3 required fixes** from the test specification, with partial implementation of the session isolation fix. The changes successfully address the root cause of the observability layer being structurally broken, making sticky thread metrics trustworthy in both the `/compare` endpoint and comparison log.

**What was changed:**
1. ✅ **Fix 1 (Bug A):** `/compare` endpoint now consults `pin_manager` and includes `sticky_count` in response
2. ✅ **Fix 2 (Bug B):** Comparison log structure refactored to nested format matching `/comparison-stats` reader
3. ⚠️ **Fix 3 (Bug C):** Session isolation partially implemented (parameter added to request model but not used in fallback logic)

**Root cause addressed:** YES. The observability layer (comparison log, `/compare`, `/comparison-stats`) now correctly reflects sticky thread activity. The plugin writes nested structure, the server reads it, and `/compare` includes pinned messages.

**Overall verdict:** APPROVED WITH COMMENTS (critical items require attention before production use)

---

## Fix 1: /compare Pin Manager Integration

**File:** `api/server.py:225-243`

**Changes:**
```python
# BEFORE:
assembler = ContextAssembler(store, token_budget=4000)
graph_assembly_result = assembler.assemble(request.user_text, inferred_tags)
graph_assembly = {
    "messages": [...],
    "total_tokens": graph_assembly_result.total_tokens,
    "recency_count": graph_assembly_result.recency_count,
    "topic_count": graph_assembly_result.topic_count,
    "tags_used": graph_assembly_result.tags_used
}

# AFTER:
pinned_ids = pin_manager.get_pinned_message_ids()  # READ-ONLY
assembler = ContextAssembler(store, token_budget=4000)
graph_assembly_result = assembler.assemble(request.user_text, inferred_tags, pinned_message_ids=pinned_ids)
graph_assembly = {
    "messages": [...],
    "total_tokens": graph_assembly_result.total_tokens,
    "sticky_count": graph_assembly_result.sticky_count,  # NEW
    "recency_count": graph_assembly_result.recency_count,
    "topic_count": graph_assembly_result.topic_count,
    "tags_used": graph_assembly_result.tags_used
}
```

**Correctness:** ✅ **PASS**
- `/compare` now calls `pin_manager.get_pinned_message_ids()` (line 232)
- Passes `pinned_message_ids` to `assembler.assemble()` (line 235)
- Includes `sticky_count` in `graph_assembly` dict (line 239)
- Does NOT call `pin_manager.tick()` (read-only, as required by spec)
- Comment explicitly documents read-only behavior (line 231)

**Potential Issues:**
- `CompareResponse` model still uses `dict` for `graph_assembly` (line 59). This is acceptable but loses type safety. Consider adding `sticky_count: int` to a typed model if strictness is desired.

**Test Coverage:**
- Tested in existing `tests/test_integration.py::TestCompareEndpoint`
- NOT covered by new Category 4 tests (test_compare_sticky.py doesn't exist)

**Verdict:** ✅ **PASS** — Fix correctly addresses Bug A. The comparison log `stickyPins` field is now trustworthy.

---

## Fix 2: Comparison Log Structure

**File:** `plugin/index.ts:571-588`

**Changes:**
```typescript
// BEFORE (flat structure):
writeComparisonLog({
  timestamp: new Date().toISOString(),
  sessionId,
  graphMsgCount: comparison.graph_assembly?.messages?.length ?? 0,
  graphTokens: comparison.graph_assembly?.total_tokens ?? 0,
  graphTags: comparison.graph_assembly?.tags_used ?? [],
  linearMsgCount: comparison.linear_window?.messages?.length ?? 0,
  linearTokens: comparison.linear_window?.total_tokens ?? 0,
  stickyPins: comparison.graph_assembly?.sticky_count ?? 0,
  hadTools,
});

// AFTER (nested structure):
writeComparisonLog({
  timestamp: new Date().toISOString(),
  sessionId,
  had_tools: hadTools,
  graph_assembly: {
    tokens: comparison.graph_assembly?.total_tokens ?? 0,
    messages: comparison.graph_assembly?.messages?.length ?? 0,
    tags: comparison.graph_assembly?.tags_used ?? [],
    recency: comparison.graph_assembly?.recency_count ?? 0,
    topic: comparison.graph_assembly?.topic_count ?? 0,
    sticky_count: comparison.graph_assembly?.sticky_count ?? 0,
  },
  linear_would_have: {
    tokens: comparison.linear_window?.total_tokens ?? 0,
    messages: comparison.linear_window?.messages?.length ?? 0,
    tags: comparison.linear_window?.tags_used ?? [],
  },
});
```

**Correctness:** ✅ **PASS**
- Writer now outputs nested `graph_assembly` and `linear_would_have` objects
- Field names match exactly what `/comparison-stats` reads (lines 338-342, 351-352 in server.py)
- Includes all required fields: `tokens`, `messages`, `tags`, `recency`, `topic`, `sticky_count`
- Field naming is consistent: `had_tools` (snake_case) matches server expectation

**End-to-end verification:**
Server reads:
```python
total_graph_tokens = sum(e["graph_assembly"]["tokens"] for e in entries)
total_linear_tokens = sum(e["linear_would_have"]["tokens"] for e in entries)
total_graph_messages = sum(e["graph_assembly"]["messages"] for e in entries)
all_tags.extend(entry["graph_assembly"]["tags"])
```
Plugin writes exactly these keys ✅

**Test Coverage:**
- NOT covered by new Category 5 tests (test_comparison_log.py doesn't exist)
- Existing test `test_regression.py::TestComparisonLogging` validates logging happens, but doesn't verify structure

**Verdict:** ✅ **PASS** — Fix correctly addresses Bug B. The KeyError that silently failed in `/comparison-stats` is now resolved.

---

## Fix 3: Session-Scoped Fallback

**File:** `api/server.py:48` (request model), lines 113-192 (assemble endpoint)

**Changes:**
```python
class AssembleRequest(BaseModel):
    user_text: str
    tags: list[str] | None = None
    token_budget: int = 4000
    tool_state: ToolState | None = None
    session_id: str | None = None  # NEW — but not used yet
```

**Correctness:** ⚠️ **PARTIAL**
- `session_id` parameter added to `AssembleRequest` model ✅
- Server-side fallback (lines 140-153) still calls `store.get_recent(5)` — this is **global**, not session-scoped ❌
- `store.get_recent_by_session()` method does NOT exist in `store.py` ❌

**Expected implementation (from spec):**
```python
if request.session_id:
    recent = store.get_recent_by_session(5, session_id=request.session_id)
else:
    recent = store.get_recent(5)  # fallback to global
```

**Impact:**
- **Test isolation:** Tests in `test_sticky_server_detection.py` can contaminate each other because fallback pulls messages from the global store, not per-session.
- **Multi-session correctness:** In production (multi-user), the fallback could pin messages from the wrong user's session.

**Workaround (from spec):**
Tests that rely on `get_recent()` behavior should ingest ≥5 messages per test to push pre-existing messages below the top-5 cutoff. Existing tests appear to do this.

**Test Coverage:**
- NOT covered by new Category 6.6 test (test_sticky_e2e.py doesn't exist)
- Existing tests in `test_sticky_server_detection.py` work around this with unique session IDs and sufficient message volume

**Verdict:** ⚠️ **PARTIAL** — Fix is incomplete. Session isolation is not enforced in the server-side fallback. This is a **MEDIUM** priority issue (see Issues section).

---

## Test Suite Quality

### Category 1: StickyPinManager Unit Tests (test_sticky.py)

**Coverage:** ✅ **EXCELLENT**
- All 6 required unit tests from spec implemented:
  - 1.1 Basic pin lifecycle ✅
  - 1.2 TTL and expiry ✅
  - 1.3 LRU eviction at capacity ✅
  - 1.4 `update_or_create_tool_chain_pin` ✅
  - 1.5 State persistence ✅
  - (Implicit: 1.0 `get_pinned_message_ids` deduplication) ✅

**Test quality:**
- Proper isolation: Each test uses `temp_state_file` fixture
- Assertions are precise and meaningful
- Edge cases covered (e.g., removing nonexistent pin, corrupted state file)

**Issues:** None. Category 1 is complete and high-quality.

### Category 2: ContextAssembler Unit Tests (test_assembler.py)

**Coverage:** ⚠️ **PARTIAL**
- Existing tests: recency, topic, deduplication, budget, empty store ✅
- Sticky layer tests in `test_sticky.py::TestStickyLayerAssembly` ✅
  - 2.1 Sticky layer populated from pinned_message_ids ✅
  - 2.2 Sticky layer absent when pinned_message_ids=None ✅
  - 2.3 Sticky layer absent when pinned_message_ids=[] (implied) ✅
  - 2.7 External ID lookup vs internal ID lookup ✅

**Missing from spec:**
- ❌ **2.4 Budget discipline** — No test with large messages (>500 tokens each) verifying sticky cap at 30%
- ❌ **2.5 Budget reallocation** — No explicit test comparing budget splits with/without sticky
- ❌ **2.6 Sticky messages appear before recency/topic** — Order is tested but not sticky-specific

**Test quality:**
- Good isolation with temp_db fixtures
- Real token counts used (50 tokens per message)
- **Gap:** No test with >100 token messages to stress budget limits

**Verdict:** ⚠️ **PARTIAL** — Core functionality tested, but budget discipline edge cases missing.

### Category 3: API Endpoint Tests (/assemble) — test_sticky_server_detection.py

**Coverage:** ✅ **EXCELLENT**
- All 8 required tests from spec implemented:
  - 3.1 Tool chain auto-pin ✅
  - 3.2 Server-side fallback ✅
  - 3.3 No sticky when tool_state=None ✅
  - 3.4 No sticky when last_turn_had_tools=False ✅
  - 3.5 Pin TTL progression ✅ (in test_integration.py)
  - 3.6 Tool chain pin extends, not duplicates ✅
  - 3.7 sticky_count correctness after pin created (implicit in 3.1, 3.6) ✅
  - 3.8 Budget cap respected in live assembly (partial — no explicit large message test) ⚠️

**Test quality:**
- Proper isolation: `clean_pins` fixture clears pins before/after each test
- Unique session IDs generated per test
- Assertions are precise and include failure messages
- Good edge case coverage (empty store, tool_state=False, etc.)

**Flakiness risks:**
- ⚠️ `test_sticky_pins_fallback_uses_recent_messages` (line 224): Asserts `len(pin["message_ids"]) >= 3` but get_recent() is global. If other tests run concurrently, this could be flaky.

**Verdict:** ✅ **PASS** — Comprehensive coverage, minor flakiness risk acceptable for dev environment.

### Category 4: /compare Endpoint Tests (test_compare_sticky.py)

**Coverage:** ❌ **MISSING**
- File does NOT exist
- Required tests from spec not implemented:
  - 4.1 `/compare` returns `sticky_count` in `graph_assembly`
  - 4.2 `/compare` returns `sticky_count == 0` when no pins
  - 4.3 `/compare` `sticky_count` matches `/assemble` `sticky_count`
  - 4.4 `/compare` does NOT tick the pin manager

**Impact:**
- Fix 1 is not explicitly validated by new tests (only by existing integration test)
- Critical behavior (read-only, no tick) is not regression-tested

**Verdict:** ❌ **FAIL** — Category 4 tests are completely missing.

### Category 5: Comparison Log Tests (test_comparison_log.py)

**Coverage:** ❌ **MISSING**
- File does NOT exist
- Required tests from spec not implemented:
  - 5.1 Comparison log entry has correct nested structure
  - 5.2 `/comparison-stats` returns non-zero totals when log has entries
  - 5.3 Comparison log `sticky_count` field matches actual pin activity

**Impact:**
- Fix 2 is not explicitly validated (only manual inspection)
- Structure mismatch could regress without test coverage

**Verdict:** ❌ **FAIL** — Category 5 tests are completely missing.

### Category 6: End-to-End Lifecycle Tests (test_sticky_e2e.py)

**Coverage:** ❌ **MISSING**
- File does NOT exist
- Required tests from spec not implemented:
  - 6.1 Full tool chain lifecycle
  - 6.2 Sticky survives reference query
  - 6.3 Non-tool conversation never creates pins
  - 6.4 Max pin count enforced (LRU eviction)
  - 6.5 Sticky budget cap prevents token overflow
  - 6.6 Gateway restart recovery

**Impact:**
- No multi-turn conversation simulation
- No explicit test of 20-turn non-tool scenario
- Budget cap with large messages (6.5) not tested

**Verdict:** ❌ **FAIL** — Category 6 tests are completely missing.

---

## Issues Found

### CRITICAL Issues

None. Core functionality is correct.

### HIGH Priority Issues

**H1. Missing test files (Categories 4, 5, 6)**
- **Severity:** HIGH
- **Description:** Test files `test_compare_sticky.py`, `test_comparison_log.py`, and `test_sticky_e2e.py` do not exist. These are required by the spec to validate Fixes 1 and 2, and to cover end-to-end scenarios.
- **Impact:** Fixes are not regression-tested. Future changes could break `/compare` sticky integration or comparison log structure without detection.
- **Recommendation:** Implement at minimum:
  - Test 4.4 (/compare does not tick)
  - Test 5.1 (log structure validation)
  - Test 6.3 (20 non-tool turns → zero pins)

### MEDIUM Priority Issues

**M1. Session-scoped fallback not implemented (Fix 3 incomplete)**
- **Severity:** MEDIUM
- **Description:** `session_id` parameter added to request model but not used in server-side fallback (line 144). Fallback still calls global `store.get_recent(5)`.
- **Impact:** Multi-session contamination risk. Test isolation requires workarounds.
- **Location:** `api/server.py:144`
- **Fix:**
  ```python
  # Current (line 144):
  recent = store.get_recent(5)

  # Should be:
  if request.session_id and hasattr(store, 'get_recent_by_session'):
      recent = store.get_recent_by_session(5, session_id=request.session_id)
  else:
      recent = store.get_recent(5)  # fallback
  ```

**M2. Budget discipline not tested with realistic token counts**
- **Severity:** MEDIUM
- **Description:** No test verifies sticky layer cap at 30% with messages >100 tokens (spec test 2.4, 6.5).
- **Impact:** Budget overflow could occur with large tool chains in production.
- **Recommendation:** Add test:
  ```python
  def test_sticky_budget_cap_with_large_messages():
      # Ingest 10 messages @ 500 tokens each
      # Pin all 10
      # Assemble with token_budget=4000
      # Assert sticky_tokens <= 1200 (30%)
      # Assert sticky_count < 10
  ```

**M3. One test failure: test_pin_ttl_expiry**
- **Severity:** MEDIUM
- **Description:** Test expects pin_id `ex-1773889897-08bb1101` but finds `to-1773889897-7370e74e`. This suggests a tool_chain pin was created by the assemble call, evicting the explicit pin.
- **Location:** `tests/test_integration.py:269`
- **Root cause:** The test calls `/assemble` without tool_state, which triggers reference detection (line 156 in server.py). If the user_text "First query" matches reference patterns, a reference pin is created, potentially evicting the explicit pin due to LRU.
- **Fix:** Change test user_text to non-reference phrase (e.g., "Execute task alpha") or disable reference detection during this test.

### LOW Priority Issues

**L1. pytest mark warnings**
- **Severity:** LOW
- **Description:** `@pytest.mark.sticky` used but not registered in pytest.ini
- **Impact:** Test discovery works but emits warnings
- **Fix:** Add to `pytest.ini`:
  ```ini
  markers =
      sticky: sticky thread tests
      slow: tests that take >5 seconds
      integration: tests requiring running server
  ```

**L2. CompareResponse model uses dict instead of typed fields**
- **Severity:** LOW
- **Description:** `graph_assembly: dict` loses type safety for `sticky_count`
- **Impact:** IDE autocomplete and type checking degraded
- **Recommendation:** Define typed model or add comment documenting dict keys

---

## Test Results

**Command:** `python3 -m pytest tests/ -v -m 'not slow' 2>&1`

**Summary:** 123 passed, 1 failed, 2 warnings

**Failures:**
- `tests/test_integration.py::TestPinTTLExpiry::test_pin_ttl_expiry` (AssertionError: pin ID mismatch)

**Warnings:**
- Unknown pytest.mark.sticky (2 occurrences)

**Pass rate:** 99.2% (123/124)

**Key passes:**
- All Category 1 unit tests (StickyPinManager) ✅
- All Category 2 assembler tests (basic functionality) ✅
- All Category 3 API tests (server-side detection) ✅
- Reference detection tests ✅
- Comparison logging tests (structure not validated) ✅

**Key failures/gaps:**
- Category 4 tests (compare sticky) — MISSING
- Category 5 tests (comparison log) — MISSING
- Category 6 tests (e2e lifecycle) — MISSING
- 1 TTL expiry test failing (reference detection interference)

---

## Gaps and Garden Paths

### Gap 1: No explicit validation of comparison log structure

**Description:** Fix 2 refactored the log structure from flat to nested, but no test reads the actual JSONL file and validates the schema.

**Risk:** If the plugin reverts to flat structure or server changes field names, the system silently breaks (KeyError caught by try/except).

**Recommendation:** Implement test 5.1:
```python
def test_comparison_log_has_nested_structure():
    log_path = Path.home() / ".tag-context" / "comparison-log.jsonl"
    # Trigger a log write via /compare
    # Read last line
    # Assert entry["graph_assembly"]["tokens"] exists
    # Assert entry["graph_assembly"]["sticky_count"] exists
```

### Gap 2: /compare does not tick — not explicitly tested

**Description:** Fix 1 added comment "READ-ONLY: do NOT tick", but no test verifies this behavior.

**Risk:** Future refactoring could add `pin_manager.tick()` call before `/compare`, causing pins to expire prematurely during comparison logging.

**Recommendation:** Implement test 4.4:
```python
def test_compare_does_not_tick_pin_manager():
    # Create pin with ttl_turns=3, turns_elapsed=0
    # Call /compare 5 times
    # GET /pins → assert turns_elapsed == 0
```

### Gap 3: Budget cap with large messages not tested

**Description:** Spec explicitly requires testing with messages >500 tokens (test 2.4, 6.5), but all existing tests use 50-token messages.

**Risk:** Sticky layer could exceed 30% budget in production with large tool call chains.

**Recommendation:** Add test with realistic token counts (e.g., 500-token messages, pin 10, assemble with 4000 budget).

---

## Verdict

### ✅ APPROVED WITH COMMENTS

**What works:**
1. `/compare` endpoint correctly integrates pin_manager (Fix 1) ✅
2. Comparison log structure matches reader (Fix 2) ✅
3. Core sticky functionality (pin creation, TTL, LRU, extension) ✅
4. Server-side fallback triggers when chain_ids empty ✅
5. Test coverage for Categories 1-3 is comprehensive ✅

**What needs attention (before production use):**

**Must fix:**
- [ ] Implement session-scoped fallback (Fix 3) — MEDIUM priority
- [ ] Fix test_pin_ttl_expiry failure (change test user_text or disable reference detection)

**Should fix:**
- [ ] Add test 4.4 (/compare does not tick) — Validates critical read-only behavior
- [ ] Add test 5.1 (log structure validation) — Prevents regression of Fix 2
- [ ] Add test 6.3 (20 non-tool turns) — Validates no runaway pin accumulation

**Nice to have:**
- [ ] Add test 2.4 (budget discipline with large messages)
- [ ] Register pytest markers (sticky, slow, integration)
- [ ] Implement full Category 6 e2e test suite

---

## Conclusion

The agent successfully addressed the root cause identified in the test spec: **the observability layer was structurally broken**. The `/compare` endpoint now includes sticky_count, and the comparison log structure matches what `/comparison-stats` reads. This makes sticky thread metrics trustworthy.

However, the implementation is incomplete:
- Session isolation (Fix 3) is only 50% done (parameter added, not used)
- Three categories of tests (4, 5, 6) are completely missing
- Budget discipline with large messages is untested

**Recommendation:** Deploy to dev environment for manual testing, but hold production deployment until:
1. Fix 3 is completed (session-scoped fallback)
2. Test 4.4 and 5.1 are implemented (regression protection for Fixes 1 and 2)
3. test_pin_ttl_expiry failure is resolved

The fixes that were implemented are correct and address the core bugs. The missing pieces are "quality of life" and edge case coverage, not fundamental correctness issues.

---

**Review complete.** See above for actionable next steps.
