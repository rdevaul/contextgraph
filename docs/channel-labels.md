# Channel Labels — Cross-Channel User Identity

By default, the contextgraph plugin scopes user tags to the `senderId` provided
by the channel (e.g. Telegram user ID `994902066`). This works perfectly for
single-channel installs with no configuration needed.

For multi-channel deployments — where the same person interacts via Telegram,
Discord, Signal, etc. — sender IDs differ per channel, which would create
separate user-tag profiles for the same person. The channel labels config
provides a **many-to-one** mapping from any sender ID to a single canonical
username, unifying the profile across channels.

---

## Configuration

Two options are supported. **Config file takes precedence** over env var.
If both are set, a warning is logged at startup.

### Option 1 — Config File (recommended)

Create `<sybilclaw-config-dir>/contextgraph/channel_labels.yaml`:

```yaml
# Maps channel sender IDs to canonical usernames.
# Many-to-one: multiple IDs can map to the same user.
# Unquoted values are fine; quotes are optional.

# Rich — Telegram + Discord
"994902066": rich
"510637988242522133": rich

# Dana — Telegram
"900606288": dana

# Terry — Telegram
"7686402653": terry
```

Default config dir is `~/.sybilclaw`. Override with `SYBILCLAW_CONFIG_DIR`
or `OPENCLAW_CONFIG_DIR` env vars.

### Option 2 — Environment Variable

Set `CONTEXTGRAPH_SENDER_LABELS` as a JSON object:

```bash
export CONTEXTGRAPH_SENDER_LABELS='{"994902066":"rich","510637988242522133":"rich","900606288":"dana","7686402653":"terry"}'
```

Useful for container/CI deployments where file-based config is inconvenient.

---

## Resolution Order

1. **Structured session key** — `agent:<prefix>-<user>:<channel>` → extracts `<user>`
   (for custom deployments that emit structured session IDs)
2. **Config file or env var lookup** — `senderId → canonical username`
3. **Raw senderId** — used directly as the label (single-channel installs, no config needed)
4. **`"unknown"`** — safe fallback, no user tags loaded

---

## Notes

- If no mapping is configured and you only use one channel, everything works
  automatically — the senderId is used directly and consistently as the label.
- The server stores user tags in `USER_TAGS_DIR/{label}.yaml`, so any consistent
  string is valid as a label.
- Restart the SybilClaw gateway after editing `channel_labels.yaml` — labels
  are loaded once at plugin startup.
