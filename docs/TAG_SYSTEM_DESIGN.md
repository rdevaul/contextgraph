<!-- CURRENT: Actively maintained documentation. Last reviewed 2026-04-12. -->

# Context Graph Tag System Design

_2026-04-11 — Rich DeVaul / GLaDOS_

## Principles

1. **System tags are explicit and stable.** No auto-discovery, auto-promotion, or auto-demotion.
2. **User tags are opt-in only.** Added via explicit `/tags` command, nothing else.
3. **No magic.** If a tag changes, a human made it happen.

## System Tags

- Stored in `data/tags.yaml` (relative to tag-context root)
- Loaded at service startup — deterministic, persisted across Gateway sessions
- Format: list of tag names with optional metadata

```json
{
  "tags": [
    {"name": "ai", "state": "core"},
    {"name": "voice-pwa", "state": "core"},
    {"name": "yapCAD", "state": "core"},
    ...
  ]
}
```

- **Adding system tags:** Either edit the file + restart, or explicit `/tags add <name> system` command
- **No automatic changes.** No discovery from dropped tags, no salience-based promotion, no stale demotion.

### Auto-Discovery (Future)

Auto tag discovery is an interesting topic for future work, but is **NOT currently supported**. Future implementations will need to grapple with:

- **Tag quality:** How to distinguish meaningful patterns from noise
- **Tag diversity:** Preventing tag explosion and ensuring useful coverage
- **Non-interference/overlap:** New auto-discovered tags should not conflict with or duplicate existing system and user tags
- **Governance:** Who approves auto-discovered tags? Is there a review pipeline?

Until these challenges are solved, the system stays explicit.

## User Tags

- Stored in `~/.tag-context/tags.user.registry/<channel_label>.json`
- **Only added via explicit `/tags` command** — no mirroring from system promotions, no auto-discover
- Each user registry tracks only the tags that user has explicitly adopted
- Hit counts are tracked per tag for scoring purposes
- **Stale user registries** (e.g., old channel labels for the same person) should be cleaned up periodically

## Tag Matching

- The tagger matches incoming messages against the union of system tags
- Per-user tag context is filtered via user_registry tags during assembly
- Matching is deterministic — what you see in tags.yaml is what you get

## Commands

`/tags` — list all tags (system + user)
`/tags add <name> system` — add a system tag (restart required)
`/tags add <name> user` — add a user tag to current user's registry
`/tags pin <message_id> <tag>` — pin a message to a tag
`/tags unpin <pin_id>` — unpin

## History

- Pre-2026-04: Hybrid tag lifecycle with discovery/promotion/demotion
- 2026-04-09: Channel label merge migration broke `/tags` endpoint and `get_by_tag()` signature
- 2026-04-11: Full redesign to explicit-only system (this doc)
