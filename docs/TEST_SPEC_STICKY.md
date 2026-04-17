<!-- HISTORICAL: Mar 2026 test specification for sticky thread system. Test spec completed. Retained for reference only. Not actively maintained. -->
# Sticky Thread Test Specification

*Written: 2026-03-18*
*Status: Authoritative — drives test implementation*
*Architecture ref: `docs/STICKY_THREADS.md`*

---

## Purpose

This spec defines a comprehensive test suite for the sticky thread system. It is written
after a critical architecture review that identified the root cause of repeated debugging
failures: **sticky threads work correctly in `/assemble`, but the observability layer
(comparison log, `/compare`, `/comparison-stats`) is structurally broken**, causing
false negatives that look like sticky failures.

The spec covers:

1. What to test and why (tracing to the architecture doc)
2. Which bugs must be fixed before new tests can pass
3. Test categories, isolation requirements, and slow-test markers
4. Concrete assertions for every behavior

---

## Required Fixes (Pre-conditions for Tests to Pass)

Before this test suite is implemented, the following bugs must be fixed:

### Fix 1: `/compare` must consult `pin_manager` (Bug A)

**Reference:** `STICKY_THREADS.md §API Changes`

The `/compare` endpoint builds a `ContextAssembler` without pinned_message_ids, so
`graph_assembly.sticky_count` is always 0. The `CompareResponse` model doesn't even
declare `sticky_count`.

**Fix:** `/compare` must:
- Call `pin_manager.get_pinned_message_ids()`
- Pass them to `assembler.assemble()`
- Include `sticky_count` in `graph_assembly` dict
- Add `sticky_count: int` to `CompareResponse` model (or use `dict` with documented fields)

**Impact:** Makes comparison log's `stickyPins` field trustworthy.

### Fix 2: Comparison log field structure must match `/comparison-stats` reader (Bug B)

**Reference:** Plugin `writeComparisonLog()` vs server `/comparison-stats` endpoint

The plugin writes flat fields (`graphTokens`, `linearTokens`); the stats endpoint
reads nested fields (`entry["graph_assembly"]["tokens"]`, `entry["linear_would_have"]["tokens"]`).
This causes `KeyError` on every stats read, silently caught by the `try/except`.

**Fix (two options, pick one):**
- **Option A:** Change `writeComparisonLog()` to write nested structure:
  ```json
  {
    "graph_assembly": { "tokens": 3423, "messages": 23, "tags": [...], "sticky_count": 1 },
    "linear_would_have": { "tokens": 3717, "messages": 22, "tags": [...] },
    "had_tools": true,
    "timestamp": "...",
    "sessionId": "..."
  }
  ```
- **Option B:** Change `/comparison-stats` to read the flat structure.

Option A is preferred: it is the structure implied by the dashboard design and
matches what `/comparison-log` endpoint is supposed to serve.

### Fix 3: Server-side fallback should be session-scoped (Bug C)

**Reference:** `STICKY_THREADS.md §What Gets Pinned — Active Tool Chains`

When `pending_chain_ids` is empty (post-restart), the server calls `get_recent(5)`
which is globally scoped. In single-session use this is fine; in multi-session
(or test) environments it contaminates pins with unrelated messages.

**Fix:** `/assemble` request should include `session_id` (optional). When provided,
the fallback uses `store.get_recent_by_session(5, session_id=session_id)` if the
store supports it, otherwise fall back to global `get_recent(5)`.

Note: If `store.get_recent_by_session()` doesn't exist, add it (or filter in the
endpoint). This is important for test isolation.

---

## Test Categories

### Category 1: Unit Tests — `StickyPinManager` (no HTTP)

**File:** `tests/test_sticky.py` (add/extend existing)

These tests import `StickyPinManager` directly. No server required.

#### 1.1 Basic pin lifecycle
- Create pin → verify it's in `get_active_pins()`
- Remove pin → verify it's gone
- Get nonexistent pin → returns `None`
- `get_pinned_message_ids()` returns flat deduplicated list

#### 1.2 TTL and expiry
- Create pin with `ttl_turns=3`
- Call `tick()` three times → pin should appear in expired list and be removed
- Verify `get_active_pins()` returns empty after expiry
- Verify `_save_state()` is called after expiry (file reflects new state)

#### 1.3 LRU eviction at capacity
- Create `MAX_ACTIVE_PINS` (5) pins
- Create one more → oldest should be evicted
- `len(get_active_pins()) == MAX_ACTIVE_PINS`
- Evicted pin is the one with the smallest `created_at`

#### 1.4 `update_or_create_tool_chain_pin`
- No existing tool_chain pin → creates new one
- Existing tool_chain pin → extends message_ids, resets `turns_elapsed`, updates tokens
- Second call with overlapping IDs → no duplicates in `message_ids`
- `pin_id` is stable across updates (same pin, not replaced)

#### 1.5 State persistence
- Create pins, instantiate new `StickyPinManager` from same path → pins survive
- Corrupted state file → manager starts fresh without crashing

### Category 2: Unit Tests — `ContextAssembler` (no HTTP)

**File:** `tests/test_assembler.py` (add/extend existing)

These tests use a real `MessageStore` (in-memory or temp SQLite) and call
`assemble()` directly.

**Reference:** `STICKY_THREADS.md §Assembler Changes`

#### 2.1 Sticky layer is populated from `pinned_message_ids`
- Insert 10 messages into store
- Pin 3 specific message IDs
- Call `assemble(pinned_message_ids=[...])` → result.sticky_count == 3
- Pinned messages appear in result.messages
- `AssemblyResult.sticky_count` matches len(pinned messages that fit in budget)

#### 2.2 Sticky layer absent when `pinned_message_ids=None`
- Insert 10 messages
- Call `assemble(pinned_message_ids=None)` → result.sticky_count == 0
- No sticky layer, full budget goes to recency + topic (original two-layer behavior)

#### 2.3 Sticky layer absent when `pinned_message_ids=[]`
- Empty list → same as None → sticky_count == 0
- This is the "no active pins" normal case

#### 2.4 Budget discipline — sticky never exceeds 30%
- Create a message store with large messages (>500 tokens each)
- Pin 10 messages that would together exceed 30% of budget
- Call `assemble()` with `token_budget=4000`
- Verify `sticky_tokens <= 1200` (30% of 4000)
- Verify `result.sticky_count < 10` (budget cap truncated the list)

#### 2.5 Budget reallocation when sticky is empty
- With sticky empty: recency + topic should together get 100% of budget
- With sticky active: remaining budget (70%+) splits between recency and topic
- (This validates the budget math in `assembler.py`)

#### 2.6 Sticky messages always appear before recency/topic in final output
- After `assemble()`, messages are sorted oldest-first
- But ALL sticky messages should be included (not crowded out by recency/topic)
- Confirm sticky_count + recency_count + topic_count == len(result.messages)

#### 2.7 External ID lookup vs internal ID lookup
- Ingest a message with `external_id="ext-abc"` and internal `id="int-xyz"`
- Pin `"ext-abc"` → assembler finds the message (via `get_by_external_id`)
- Pin `"int-xyz"` → assembler finds the message (via fallback `get_by_id`)
- Pin `"nonexistent-id"` → gracefully skipped, no crash

### Category 3: API Endpoint Tests — `/assemble` (require running server)

**File:** `tests/test_sticky_server_detection.py` (extend existing)

These tests hit the live API at `http://localhost:8302`.

#### 3.1 Tool chain auto-pin (existing, keep)
- `tool_state.last_turn_had_tools=True`, `pending_chain_ids=[ext_id1, ext_id2]`
- Response: `sticky_count > 0`
- `GET /pins` shows one `tool_chain` pin with those IDs

#### 3.2 Server-side fallback (existing, improve isolation)
- `tool_state.last_turn_had_tools=True`, `pending_chain_ids=[]`
- Response: `sticky_count > 0` (if messages exist in store)
- Pin contains recent messages

#### 3.3 No sticky when `tool_state=None` (existing, keep)
- Response: `sticky_count == 0`
- No `tool_chain` pins created

#### 3.4 No sticky when `tool_state.last_turn_had_tools=False` (existing, keep)
- Response: `sticky_count == 0`

#### 3.5 Pin TTL progression
- Create pin via tool_state
- Call `/assemble` (no tool_state) N times, check `turns_elapsed` increments
- After `ttl_turns` calls: pin is gone, `expired_pins` list includes the pin_id

#### 3.6 Tool chain pin extends, not duplicates
- Call `/assemble` with `tool_state` twice → still only 1 pin (existing, improve)
- Verify pin_id is stable
- Verify `turns_elapsed` resets to 0 (or 1, accounting for tick)

#### 3.7 **NEW: `sticky_count` correctness after pin created**
- Ingest 3 messages with known IDs
- Call `/assemble` with `tool_state` (creates pin)
- Call `/assemble` again with no tool_state
- Second call: `sticky_count > 0` because pin persists from first call
- This is the "continuity" test: sticky persists across the turn where it was created

#### 3.8 **NEW: Budget cap respected in live assembly**
- Ingest messages with large content (>100 tokens each)
- Pin 10 of them explicitly via `POST /pin`
- Call `/assemble` with small budget (e.g. `token_budget=400`)
- `sticky_count < 10` (cap hit)
- `total_tokens <= 400` (budget not exceeded)

### Category 4: API Endpoint Tests — `/compare` (require running server)

**File:** `tests/test_compare_sticky.py` (NEW FILE)

These tests validate the fixed `/compare` endpoint.

**Reference:** Fix 1 (pre-condition)

#### 4.1 `/compare` returns `sticky_count` in `graph_assembly`
- Call `POST /pin` to create an explicit pin
- Call `POST /compare` with user_text and assistant_text
- Response: `graph_assembly.sticky_count > 0`
- This test FAILS before Fix 1 is applied

#### 4.2 `/compare` returns `sticky_count == 0` when no pins
- Ensure no pins exist (via clean_pins fixture)
- Call `POST /compare`
- `graph_assembly.sticky_count == 0`

#### 4.3 `/compare` `sticky_count` matches `/assemble` `sticky_count`
- Create pin via `/assemble` with tool_state
- Call `/compare` with same `user_text`
- `compare.graph_assembly.sticky_count == assemble.sticky_count`
- These should agree (modulo the tick — compare doesn't tick)

#### 4.4 `/compare` does NOT tick the pin manager
- Create pin with `ttl_turns=3`, `turns_elapsed=0`
- Call `/compare` 5 times
- Pin still exists (not expired) — compare is read-only, must not tick
- `GET /pins` shows `turns_elapsed == 0` (unchanged)

### Category 5: Comparison Log Tests (require running server + log file)

**File:** `tests/test_comparison_log.py` (NEW FILE)

These tests validate that the comparison log written by the plugin has the
correct structure and that `/comparison-stats` can read it.

**Reference:** Fix 2 (pre-condition)

#### 5.1 Comparison log entry has correct nested structure
- Write a synthetic log entry (or trigger via a controlled `/afterTurn` simulation)
- Read `~/.tag-context/comparison-log.jsonl`
- Verify entry has `graph_assembly.tokens`, `graph_assembly.messages`,
  `graph_assembly.sticky_count`, `linear_would_have.tokens`, etc.
- This test FAILS before Fix 2 is applied

#### 5.2 `/comparison-stats` returns non-zero totals when log has entries
- Ensure log has at least one entry
- Call `GET /comparison-stats`
- `total_turns > 0`, `avg_graph_tokens > 0`
- No 500 errors from KeyError

#### 5.3 Comparison log `stickyPins` field matches actual pin activity
- Create a tool chain via `/assemble` with `tool_state`
- Simulate what the plugin's `afterTurn` writes to the log
- Verify `graph_assembly.sticky_count > 0` in the log entry
- This test FAILS before Fix 1 and Fix 2 are both applied

### Category 6: End-to-End Lifecycle Tests — `@pytest.mark.slow`

**File:** `tests/test_sticky_e2e.py` (NEW FILE)

These tests simulate full multi-turn conversations and may take several seconds.
Mark with `@pytest.mark.slow` and `@pytest.mark.integration`.

#### 6.1 Full tool chain lifecycle

Simulate:
1. Turn 1: Ingest "deploy app" / "deploying..." → `/assemble` with tool_state → pin created
2. Turn 2: Ingest "check status" / "checking..." → `/assemble` with tool_state → pin extended
3. Turn 3: Ingest "done" / "deployment complete" → `/assemble` WITHOUT tool_state → pin starts aging
4. Turn N (TTL+1): Pin expires, `sticky_count == 0`

Assert at each step:
- Pin ID is stable across turns 1-2
- `turns_elapsed` increments on non-tool turns
- Messages from turn 1 appear in assembled context on turns 2-3

#### 6.2 Sticky survives reference query

After a tool chain:
1. Ingest follow-up "any updates on that?" → `/assemble` with no tool_state
2. `detect_reference()` should trigger a reference pin
3. `sticky_count > 0` even without explicit tool_state

#### 6.3 Non-tool conversation never creates pins

Run 20 turns of normal Q&A (no `tool_state`):
- `GET /pins` after each turn → `total_pins == 0`
- `sticky_count == 0` on every `/assemble` response
- No accumulation of reference pins (each reference pin expires, no runaway growth)

#### 6.4 Max pin count enforced (LRU eviction)

1. Create 5 explicit pins via `POST /pin`
2. Create a 6th → verify `total_pins == 5` (oldest evicted)
3. The oldest pin (by `created_at`) is gone from `GET /pins`

#### 6.5 Sticky budget cap prevents token overflow (slow)

1. Ingest 10 large messages (each ~500 tokens)
2. Pin all 10
3. Assemble with `token_budget=4000`
4. `total_tokens <= 4000` — strictly enforced
5. `sticky_count <= 2` (at most ~1200 tokens / ~500 tokens per msg)
6. `recency_count + topic_count > 0` — other layers still get budget

#### 6.6 Gateway restart recovery

Simulate by calling `/assemble` with:
- `tool_state.last_turn_had_tools=True`
- `pending_chain_ids=[]` (simulates plugin memory wiped)
- But messages exist in store from previous turns

Assert:
- `sticky_count > 0` (server-side fallback activated)
- Pin reason contains "fallback"

---

## Test Infrastructure Requirements

### Isolation

The current test suite has a critical isolation gap: `get_recent()` is global.

**Required changes:**

1. Add `session_id` parameter to `/assemble` request (optional, defaults to `None`)
2. Add `store.get_recent_by_session(n, session_id)` to `MessageStore`
3. Server-side fallback uses session-scoped `get_recent` when `session_id` provided
4. Test fixture generates unique session IDs per test AND passes them in assemble calls

Until this is fixed, the `clean_pins` fixture is insufficient — tests that rely
on "no messages exist" will be flaky if the store has messages from other tests.

**Interim workaround:** Tests that care about `get_recent()` behavior should
ingest enough messages in the test that any pre-existing messages are below the
top-5 cutoff (i.e., ingest 5 or more messages per test).

### Markers

```python
# pytest.ini additions:
markers =
    sticky: sticky thread tests
    slow: tests that take >5 seconds
    integration: tests requiring running server
    compare: tests for /compare endpoint
```

### Fixtures

```python
@pytest.fixture
def unique_session():
    """Generate unique session ID for test isolation."""
    return f"test-{uuid.uuid4()}"

@pytest.fixture(autouse=True)
def clean_pins_and_check_server(api_available):
    """Clear all pins before and after each server test."""
    ...

@pytest.fixture
def temp_log_path(tmp_path):
    """Use a temp log path for comparison log tests."""
    return tmp_path / "comparison-log.jsonl"
```

---

## Mapping: Bugs → Tests That Catch Them

| Bug | Root Cause | Test That Catches It |
|-----|-----------|----------------------|
| Bug A: `/compare` stickyPins always 0 | CompareResponse missing sticky_count | 4.1, 4.3, 5.3 |
| Bug B: Comparison log field mismatch | Writer/reader disagree on structure | 5.1, 5.2 |
| Bug C: Fallback not session-scoped | `get_recent()` is global | 6.6 + isolation fix |
| Bug D: Test store not isolated | No per-test message store reset | 3.2 improved, 6.1 |
| Bug E: tick() before pin creation in test | Tick happens at start of /assemble | 3.6 revised assertion |

---

## Test Execution Order Recommendation

```bash
# Fast unit tests (no server required):
python3 -m pytest tests/test_sticky.py tests/test_assembler.py -v

# API tests (server must be running):
python3 -m pytest tests/test_sticky_server_detection.py tests/test_compare_sticky.py -v -m "not slow"

# Comparison log tests:
python3 -m pytest tests/test_comparison_log.py -v

# E2E slow tests (allow 60s+):
python3 -m pytest tests/test_sticky_e2e.py -v -m slow --timeout=120
```

---

## Success Criteria

After all fixes and tests are implemented:

1. `python3 -m pytest tests/ -v` passes with 0 failures
2. `GET /comparison-stats` returns valid data (no 500 errors)
3. `GET /comparison-log` entries have `graph_assembly.sticky_count` field
4. After a tool-using turn, the NEXT turn's `/compare` response shows `sticky_count > 0`
5. Non-tool conversations produce zero pins
6. The dashboard accurately reflects whether sticky is active

---

*End of spec*
