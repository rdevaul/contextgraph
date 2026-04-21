"""
config.py — Environment variable configuration for contextgraph.

All paths and agent-specific settings are controlled via environment variables,
allowing multiple agents (GLaDOS, Jarvis, etc.) to run independent instances
on the same machine.
"""

import os
from pathlib import Path


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _resolve_workspace_default() -> str:
    """Auto-detect workspace root for openclaw or sybilclaw installations."""
    home = Path.home()
    for platform_dir in (".sybilclaw", ".openclaw"):
        candidate = home / platform_dir / "workspace"
        if candidate.is_dir():
            return str(candidate)
    # Fallback — sybilclaw is the current default
    return str(home / ".sybilclaw" / "workspace")


# Workspace root — the agent workspace directory
WORKSPACE = Path(_env(
    "CONTEXTGRAPH_WORKSPACE",
    _resolve_workspace_default()
))

# Agent name — used for launchd service naming and logging
AGENT_NAME = _env("CONTEXTGRAPH_AGENT_NAME", "agent")

# Database path
DB_PATH = Path(_env(
    "CONTEXTGRAPH_DB_PATH",
    str(Path.home() / ".tag-context" / "store.db")
))

# Tags config path
TAGS_CONFIG = Path(_env(
    "CONTEXTGRAPH_TAGS_CONFIG",
    str(Path(__file__).parent / "tags.yaml")
))

# Tagger mode controls which taggers are active in the ensemble.
# Production uses "fixed" mode only — FixedTagger + baseline tagger.
# Deterministic, auditable, fast. No additional dependencies required.
TAGGER_MODE = _env("CONTEXTGRAPH_TAGGER_MODE", "fixed")
