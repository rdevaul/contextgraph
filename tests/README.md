# Context Graph Test Suite

Comprehensive integration and regression tests for the context graph system.

## Test Structure

- **tests/test_integration.py** — 10 integration tests that verify the full Python API pipeline
- **tests/test_regression.py** — 7 regression tests for specific bugs
- **tests/test_plugin_contract.py** — 6 tests verifying TypeScript plugin contract
- **tests/test_e2e_smoke.py** — End-to-end smoke test simulating multi-turn conversation
- **tests/conftest.py** — Pytest configuration and markers

## Requirements

```bash
pip3 install --break-system-packages pytest requests
```

## Running the Tests

### Start the API Server

The integration and E2E tests require the API server to be running on port 8300:

```bash
# In one terminal, start the server
cd /Users/rich/Projects/tag-context
python3 api/server.py
```

**NOTE:** Make sure to update `api/server.py` line 517 to use port 8300:
```python
uvicorn.run(app, host="0.0.0.0", port=8300)  # Change from 8350 to 8300
```

### Run All Tests

```bash
python3 -m pytest tests/ -v --tb=short
```

### Run Specific Test Categories

```bash
# Integration tests only (require API server)
python3 -m pytest tests/ -m integration -v

# Regression tests only (require API server)
python3 -m pytest tests/ -m regression -v

# Plugin contract tests only (no API server needed)
python3 -m pytest tests/ -m plugin_contract -v

# E2E smoke test only (require API server)
python3 -m pytest tests/ -m e2e -v
```

### Run Without API Server

To skip tests that require the API server:

```bash
python3 -m pytest tests/ -m "not integration and not regression and not e2e" -v
```

## Test Coverage

### Integration Tests (tests/test_integration.py)

1. **Health check** — GET /health returns 200 with messages_in_store and tags
2. **Tag inference** — POST /tag returns valid tags
3. **Basic assembly** — POST /assemble returns messages with recency_count + topic_count
4. **Assembly with tool_state** — POST /assemble with tool_state creates sticky pins
5. **Pin lifecycle** — POST /pin creates, GET /pins lists, POST /unpin removes
6. **Pin TTL expiry** — Verify pins expire after ttl_turns
7. **Three-layer budget** — Verify sticky_count > 0 and budget split correctly
8. **Compare endpoint** — POST /compare returns both graph and linear assembly
9. **Registry** — GET /registry returns tag registry with core/candidate/archived tiers
10. **Graceful degradation** — Assembly works when no pins exist (sticky_count=0)

### Regression Tests (tests/test_regression.py)

1. **tags:null handling** — POST /assemble with tags=null should NOT return 422
2. **Content array extraction** — Test string input handling
3. **Token budget cap** — Verify assembly respects budget (max 8000, not 200k)
4. **Empty user text** — POST /assemble with user_text="" should not crash
5. **Large tool_state** — POST /assemble with 50+ pending_chain_ids should not timeout
6. **Concurrent pins** — Create 6 pins, verify LRU eviction keeps max 5
7. **Comparison logging** — Verify /compare and /comparison-log endpoints work

### Plugin Contract Tests (tests/test_plugin_contract.py)

1. **engine.ts contains detectToolChains** — Verify method exists
2. **engine.ts passes toolState to client.assemble** — Verify 4th argument
3. **api-client.ts assemble() accepts toolState** — Verify ToolState type
4. **api-client.ts sends tool_state in request body** — Verify tool_state in JSON
5. **engine.ts handles content arrays** — Verify Array.isArray handling
6. **index.ts registers context engine** — Verify registerContextEngine call

### E2E Smoke Test (tests/test_e2e_smoke.py)

Multi-turn conversation simulation:
1. POST /assemble with "deploy the app" (no tool state) → normal assembly
2. POST /assemble with tool_state.last_turn_had_tools=true → sticky pin created
3. GET /pins → verify 1 active pin
4. POST /assemble with follow-up query → sticky pin still active
5. Multiple turns pass → pin expires
6. GET /pins → verify pin expired

## Known Issues

If you see failures with KeyError: 'sticky_count', this indicates the API server on port 8300 is an older version that doesn't have sticky pin support. Make sure you're running the latest version of api/server.py.

## Pytest Markers

- `@pytest.mark.integration` — Requires API server running
- `@pytest.mark.regression` — Regression test for specific bug
- `@pytest.mark.plugin_contract` — Plugin file verification (no server needed)
- `@pytest.mark.e2e` — End-to-end smoke test (requires API server)
