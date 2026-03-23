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


# Workspace root — the agent workspace directory
WORKSPACE = Path(_env(
    "CONTEXTGRAPH_WORKSPACE",
    str(Path.home() / ".openclaw" / "workspace")
))

# Agent name — used for launchd service naming and logging
AGENT_NAME = _env("CONTEXTGRAPH_AGENT_NAME", "glados")

# MEMORY.md location
MEMORY_FILE = Path(_env(
    "CONTEXTGRAPH_MEMORY_FILE",
    str(WORKSPACE / "MEMORY.md")
))

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

# Tagger mode: "fixed" | "hybrid" | "gp-only"
TAGGER_MODE = _env("CONTEXTGRAPH_TAGGER_MODE", "fixed")
