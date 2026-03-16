# OpenClaw Native Context Engine Plugin

This directory contains an OpenClaw plugin that bridges the ContextEngine interface to the contextgraph Python FastAPI server.

## What it does

When installed as an OpenClaw extension, this plugin replaces the default linear context window with graph-based, semantically-tagged context assembly — routing through the Python API at `http://localhost:8300`.

**Graph mode is OFF by default.** The plugin acts as a transparent pass-through until explicitly enabled, so there's zero risk to existing behavior.

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
- The contextgraph Python API server running on port 8300 (see `../api/` and `../service/`)

## Installation

Copy or symlink this directory into `~/.openclaw/extensions/contextgraph/`, then restart the OpenClaw gateway.
