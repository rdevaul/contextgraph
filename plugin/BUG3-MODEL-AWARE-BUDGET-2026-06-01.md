# ContextGraph Bug 3 Fix — Model-Aware GRAPH_TOKEN_BUDGET (2026-06-01)

## Summary

The ContextGraph plugin's `assemble()` hook was clamping the model's full
context window down to a hardcoded 32K for retrieval budget, regardless of how
large the model's actual window was. For Opus/Sonnet-class models (~200K
window) this meant ContextGraph never used more than 32K of retrieval budget.

The incoming `tokenBudget` arg **is** the model's full context window (traced
by jarvis-rich: `resolveContextWindowInfo().tokens` → `ctxInfo.tokens` →
`contextTokenBudget`/`tokenBudget`, verified at `attempt.ts:1009` and
`compact.queued.ts:166`). The fix computes the graph budget as a fraction of
that window, with a floor and ceiling — no new model metadata plumbing needed.

## Before / After (budget computation in `assemble()`)

### Before
```js
const GRAPH_TOKEN_BUDGET = 32000;
const budget = Math.min(tokenBudget ?? GRAPH_TOKEN_BUDGET, GRAPH_TOKEN_BUDGET);
```
`Math.min(..., 32000)` clamped any window > 32K down to 32K. Always 32K for
Opus/Sonnet.

### After
```js
// Model-aware budget (implemented 2026-06-01): the host passes the
// model's FULL context window as `tokenBudget`. ContextGraph claims a
// FRACTION of it for retrieval, leaving the rest for the live turn +
// response. Replaces the old hardcoded 32K clamp.
//   - FRACTION (25%): share of the window reserved for retrieval.
//   - FLOOR (32K): never drop below the prior fixed value (small models).
//   - CEIL (120K): sanity cap so a giant window doesn't starve the live turn.
const GRAPH_BUDGET_FRACTION = 0.25;
const GRAPH_TOKEN_BUDGET_FLOOR = 32000;
const GRAPH_TOKEN_BUDGET_CEIL = 120000;
const modelWindow =
  typeof tokenBudget === "number" && Number.isFinite(tokenBudget) && tokenBudget > 0
    ? tokenBudget
    : null;
const budget = modelWindow
  ? Math.min(
      GRAPH_TOKEN_BUDGET_CEIL,
      Math.max(GRAPH_TOKEN_BUDGET_FLOOR, Math.floor(modelWindow * GRAPH_BUDGET_FRACTION)),
    )
  : GRAPH_TOKEN_BUDGET_FLOOR;
```

## Effective budgets

| Model window | 25% raw | After floor/ceil | Change vs before |
|---|---|---|---|
| Opus (200K) | 50,000 | **50,000** | 32K → 50K |
| Sonnet (200K) | 50,000 | **50,000** | 32K → 50K |
| Hypothetical 32K model | 8,000 | **32,000** (floor) | unchanged (32K) |
| Tiny/unknown window (null/0) | — | **32,000** (fallback) | unchanged (32K) |
| Hypothetical 1M model | 250,000 | **120,000** (ceil) | capped, keeps live-turn room |

The server-side `/assemble` layer (recency floor + 25/75 recency/topic split)
is unchanged and further splits this budget across layers.

## Files edited + build/deploy

1. **Source of record:** `~/Projects/contextgraph/plugin/index.ts` — edited ✅
2. **Deployed source copy:** `~/.openclaw-Jarvis/extensions/contextgraph/index.ts` — edited ✅
3. **Compiled & deployed:** rebuilt with
   `node_modules/.bin/tsc --noEmitOnError false --skipLibCheck --outDir /tmp/cg-plugin-dist`
   (pre-existing missing-`@types` TS7006 errors are expected, not real bugs),
   then `index.js` copied to
   `~/.openclaw-Jarvis/extensions/contextgraph/dist/index.js` — deployed ✅

### Verification (static)
- Both `index.ts` files contain `GRAPH_BUDGET_FRACTION` (2 occurrences each). ✅
- Deployed `dist/index.js` contains the new fraction/floor/ceil logic at L630–638. ✅
- Old `const GRAPH_TOKEN_BUDGET = 32000` clamp removed from dist (0 matches). ✅
- `inferSubchannelLabel` present in rebuilt dist (6 occurrences) — build sanity. ✅
- `/tmp/cg-plugin-dist/index.js` byte-identical to deployed `dist/index.js`. ✅

## Action required

**A gateway restart is required for this to take effect** — SybilClaw loads the
plugin's `dist/index.js` at startup. Rich controls restart timing. No git commit
was made (per task constraints).
