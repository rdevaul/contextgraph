<!-- HISTORICAL: Mar 2026 architecture doc for sticky threads. Design is implemented; some details may be stale. Retained for reference only. Not actively maintained. -->
# Sticky Threads — Context Assembly Enhancement

*Written: 2026-03-15*
*Status: P0 — Critical for operational reliability*

---

## Problem

Graph-mode context assembly uses two layers: **recency** (N most recent messages)
and **topic** (messages matching inferred tags). This works well for single-topic
conversations but breaks down during multi-step operational work.

**Failure mode observed (2026-03-15):**
During concurrent tasks (Voice PWA fixes, Railway deployment, domain registration,
dashboard work), the agent would:
1. Commit to an action ("deploying to Railway now")
2. Execute tool calls (exec, process, etc.)
3. On the next turn, graph assembly would rebuild context from scratch
4. The previous tool call chain would be dropped (not in recency window,
   not matching the new message's topic tags)
5. Agent loses track of in-progress work, responds with "let me check"

This is a **critical reliability failure** for any agent doing operational work.
It must be fixed before graph mode can be trusted for production use or
deployment at DML.

---

## Solution: Three-Layer Context Assembly

Add a **sticky layer** that pins active work-in-progress into the context
window regardless of recency or topic scoring.

```
Assembled context = [sticky layer] + [recency layer] + [topic layer]
```

### Layer Budget Allocation

| Layer | Budget | When sticky active | When no sticky |
|-------|--------|-------------------|----------------|
| Sticky | up to 30% | Active chains pinned | 0% (reallocated) |
| Recency | 20% | Most recent messages | 25% |
| Topic | 50% | Tag-retrieved messages | 75% |

When nothing is sticky, the budget automatically reallocates to the existing
two-layer split. The sticky layer is only "expensive" when there's active
work to preserve.

### What Gets Pinned

**1. Active Tool Chains (automatic)**
If the most recent assistant turn contained tool calls, the entire chain is
pinned:
- The user message that initiated the task
- All tool_use and tool_result messages in the chain
- The assistant's final response
- Any continuation messages ("still working on X")

The chain stays pinned until:
- The assistant produces a response with no tool calls (task complete)
- The user explicitly changes topic
- A configurable TTL expires (default: 10 turns)

**2. Explicit Pins (agent-controlled)**
The agent can mark specific messages as "pin this" via a metadata flag.
Use cases:
- "Keep the PRD in context while we build"
- "Remember this error message until we fix it"
- "This config is important for the next few steps"

Explicit pins have a TTL (default: 20 turns) and can be unpinned manually.

**3. Reference Continuity (heuristic)**
When the user's message contains references to recent work:
- "any updates on that?"
- "what happened with the deployment?"
- "did that work?"

The assembler detects these anaphoric references and pins the most likely
referent (the most recent substantial work thread).

---

## Architecture

### Data Model

```python
@dataclass
class StickyPin:
    pin_id: str              # Unique identifier
    message_ids: list[str]   # Messages in this pin group
    pin_type: str            # "tool_chain" | "explicit" | "reference"
    created_at: float        # Timestamp
    ttl_turns: int           # Turns until auto-unpin
    turns_elapsed: int       # Turns since pinned
    total_tokens: int        # Token cost of this pin group
    reason: str              # Why this was pinned
```

### Storage

Sticky state is **ephemeral** — stored in memory on the API server with
a JSON backup at `~/.tag-context/sticky-state.json`. It doesn't need the
permanence of the message store because pins are inherently short-lived.

```json
{
  "active_pins": [
    {
      "pin_id": "tc-2026-03-15-001",
      "pin_type": "tool_chain",
      "message_ids": ["msg-001", "msg-002", "msg-003"],
      "created_at": 1710532800,
      "ttl_turns": 10,
      "turns_elapsed": 2,
      "total_tokens": 1200,
      "reason": "Railway deployment in progress"
    }
  ],
  "total_sticky_tokens": 1200
}
```

### API Changes

**New endpoint: POST /pin**
```json
{
  "message_ids": ["msg-001"],
  "pin_type": "explicit",
  "reason": "Keep PRD in context",
  "ttl_turns": 20
}
```

**New endpoint: POST /unpin**
```json
{
  "pin_id": "tc-2026-03-15-001"
}
```

**New endpoint: GET /pins**
Returns all active pins with their status and token costs.

**Modified endpoint: POST /assemble**
New optional parameter: `tool_state`
```json
{
  "user_text": "any updates?",
  "tool_state": {
    "last_turn_had_tools": true,
    "tool_call_ids": ["tc-001", "tc-002"],
    "pending_chains": ["msg-001", "msg-002", "msg-003"]
  }
}
```

The assembler uses `tool_state` to automatically create/extend tool chain
pins.

### Assembler Changes (assembler.py)

```python
class ContextAssembler:
    def assemble(self, incoming_text, inferred_tags,
                 tool_state=None, explicit_pins=None):
        budget = self.token_budget

        # Layer 1: Sticky
        sticky_messages, sticky_tokens = self._assemble_sticky(
            tool_state, explicit_pins, max_tokens=int(budget * 0.3)
        )

        # Layer 2: Recency (from remaining budget)
        remaining = budget - sticky_tokens
        recency_budget = int(remaining * 0.3)
        recency_messages = self._assemble_recency(
            exclude=sticky_messages, max_tokens=recency_budget
        )

        # Layer 3: Topic (from remaining budget)
        topic_budget = remaining - sum(m.token_count for m in recency_messages)
        topic_messages = self._assemble_topic(
            inferred_tags,
            exclude=sticky_messages + recency_messages,
            max_tokens=topic_budget
        )

        return AssemblyResult(
            messages=sticky_messages + recency_messages + topic_messages,
            sticky_count=len(sticky_messages),
            recency_count=len(recency_messages),
            topic_count=len(topic_messages),
            # ...
        )
```

### Plugin Changes (engine.ts)

The OpenClaw plugin has access to the full message array in `assemble()`.
It can detect tool calls by checking message roles:

```typescript
async assemble(params) {
    const messages = params.messages;

    // Detect active tool chains
    const toolState = this.detectToolChains(messages);

    // Pass to Python API
    const result = await this.client.assemble(
        userText, undefined, budget, toolState
    );
}

private detectToolChains(messages: AgentMessage[]): ToolState {
    // Walk backwards through messages
    // If we find tool_use/tool_result pairs, collect them
    // If the last assistant message had tool calls, mark as active
}
```

### Reference Detection (reframing.py extension)

Add patterns for anaphoric references:
```python
REFERENCE_PATTERNS = [
    r"\b(any|what('s|s)?)\s+(update|progress|status)\b",
    r"\bdid (that|it|this) work\b",
    r"\bwhat happened with\b",
    r"\bhow('s| is| did) (that|it|the .+?) (go|going|turn out)\b",
    r"\bcan you (check|finish|continue)\b",
]
```

When detected, the assembler looks for the most recent substantial work
thread (defined as: a sequence of ≥3 messages with tool calls) and pins it.

---

## Implementation Plan

| Phase | Work | Effort |
|-------|------|--------|
| 1 | StickyPin data model + storage + API endpoints (/pin, /unpin, /pins) | ~2h |
| 2 | Modify assembler.py to support 3-layer assembly | ~2h |
| 3 | Modify engine.ts to detect tool chains and pass tool_state | ~1h |
| 4 | Reference detection patterns | ~1h |
| 5 | Update dashboard to show sticky layer stats | ~30m |
| 6 | Integration testing with graph mode ON | ~1h |

**Total: ~7-8 hours of focused coding agent work**

---

## Success Criteria

1. **No lost threads** — during multi-step tool operations, all intermediate
   results remain in context until the task completes
2. **Graceful degradation** — when sticky layer is empty, assembly behaves
   exactly as it does today
3. **Budget discipline** — sticky layer never consumes more than 30% of
   token budget, even with multiple active pins
4. **Auto-cleanup** — stale pins expire after TTL without manual intervention
5. **Observable** — dashboard shows active pins, their token cost, and TTL

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Sticky layer crowds out topic context | Hard cap at 30% budget; oldest pins evicted first |
| False positive reference detection | Conservative patterns; only pin if confidence > 0.8 |
| Pin accumulation (many open threads) | Max 5 active pins; LRU eviction beyond that |
| Token counting mismatch | Use same token estimator as other layers |

---

## Generalizability (DML Deployment)

This design is agent-agnostic. Any OpenClaw-based agent doing operational work
(DevOps, infrastructure, multi-step research) benefits from sticky threads.
The key abstraction is:

> **If the agent's last turn involved tool calls, the tool chain stays in
> context until the task resolves.**

This is a fundamental requirement for any agent that acts, not just converses.
The DML deployment should use the same plugin with the same sticky layer logic.
