# OpenClaw Native Context Engine Plugin

This directory contains an OpenClaw plugin that bridges the ContextEngine interface to the contextgraph Python FastAPI server.

## What it does

When installed as an OpenClaw extension, this plugin replaces the default linear context window with graph-based, semantically-tagged context assembly — routing through the Python API at `http://localhost:8302`.

**Graph mode is OFF by default.** The plugin acts as a transparent pass-through until explicitly enabled, so there is zero risk to existing behavior.

### Toggle graph mode

- `/graph on` — enable graph-based context assembly
- `/graph off` — disable (revert to linear windowing)
- `/graph` — show current status

## Files

- `index.ts` — Plugin implementation (ContextEngine interface bridge)
- `openclaw.plugin.json` — Plugin manifest
- `package.json` — Node package metadata

## Requirements

- OpenClaw with plugin/extension support
- Node.js ≥ 22
- The contextgraph Python API server running on port 8302 (see `../api/` and `../service/`)

## Installation

> ⚠️ **Check first — do not skip this step.**
>
> OpenClaw auto-loads plugins from `~/.sybilclaw/extensions/`. If contextgraph is already
> installed there, adding it again to `openclaw.json` will cause a **duplicate registration**
> crash-loop. Always verify before installing.

### Step 1: Check if already installed

```bash
openclaw plugins list | grep contextgraph
```

- If you see `loaded` in the Status column → **plugin is already running, stop here.**
- If you see nothing → proceed with Step 2.

### Step 2: Copy plugin files

```bash
mkdir -p ~/.sybilclaw/extensions/contextgraph
cp index.ts openclaw.plugin.json package.json ~/.sybilclaw/extensions/contextgraph/
```

### Step 3: Reload the gateway

```bash
openclaw gateway reload
```

> ⚠️ **Do NOT use `openclaw gateway restart`** unless the gateway is completely dead.
> Restart kills all active sessions (Telegram, Discord, Voice). Use `reload` for plugin
> changes — it hot-swaps the plugin with connections intact.

### Step 4: Verify

```bash
openclaw plugins list | grep contextgraph
# Should show: loaded   global:contextgraph/index.ts
```

### What NOT to do

Do **not** add this plugin to `openclaw.json` under `plugins.allow` or `plugins.entries`.
Auto-loading from `~/.sybilclaw/extensions/` is the correct and only installation path.
Adding it to config while it is already auto-loaded will crash the gateway with a duplicate
registration error.

---

## Updating the Plugin

To deploy a new version of `index.ts`:

```bash
cp index.ts ~/.sybilclaw/extensions/contextgraph/index.ts
openclaw gateway reload
```

That is all. No config changes needed.
