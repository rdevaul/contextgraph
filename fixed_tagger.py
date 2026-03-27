"""
fixed_tagger.py — User-configurable fixed-tag tagger.

Reads tags.yaml (or a path given at construction). Hot-reloads on change.
Optionally loads per-user tags from a user tag file and merges them with
system tags (user tags override system tags on name collision).

Compatible with StructuredProgramTagger.assign() interface.
"""

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

from features import MessageFeatures
from tagger import TagAssignment, _strip_metadata

DEFAULT_TAGS_PATH = Path(__file__).parent / "tags.yaml"
USER_TAGS_DIR = Path.home() / ".tag-context" / "tags.user"


@dataclass
class TagSpec:
    name: str
    keywords: List[str]
    patterns: List[re.Pattern]
    requires_all: bool
    confidence: float
    enabled: bool


def _parse_tag_specs(data: dict) -> List[TagSpec]:
    """Parse a tags YAML data dict into a list of TagSpec objects."""
    result = []
    for entry in data.get("tags", []):
        if not entry.get("enabled", True):
            continue
        compiled_patterns = []
        for p in entry.get("patterns", []):
            try:
                compiled_patterns.append(
                    re.compile(p, re.IGNORECASE | re.MULTILINE)
                )
            except re.error:
                pass
        result.append(TagSpec(
            name=entry["name"],
            keywords=[k.lower() for k in entry.get("keywords", [])],
            patterns=compiled_patterns,
            requires_all=entry.get("requires_all", False),
            confidence=entry.get("confidence", 1.0),
            enabled=True,
        ))
    return result


class FixedTagger:
    """
    Keyword/pattern-based tagger driven by a YAML config.

    Hot-reloads: if tags.yaml or the user tag file mtime changes, reloads
    automatically without restarting the service.

    User tags are loaded from ~/.tag-context/tags.user/<channel_label>.yaml
    when a channel_label is provided. User tags are merged with system tags;
    user tags take precedence on name collision.

    Interface-compatible with StructuredProgramTagger.
    """

    def __init__(self, config_path: Optional[Path] = None,
                 user_tags_path: Optional[Path] = None,
                 reload_interval: float = 30.0) -> None:
        self._path = config_path or DEFAULT_TAGS_PATH
        self._user_tags_path: Optional[Path] = user_tags_path
        self._reload_interval = reload_interval
        self._tags: List[TagSpec] = []
        self._mtime: float = 0.0
        self._user_mtime: float = 0.0
        self._lock = threading.RLock()
        self._load()

    @classmethod
    def for_channel(cls, channel_label: Optional[str],
                    config_path: Optional[Path] = None,
                    reload_interval: float = 30.0) -> "FixedTagger":
        """
        Convenience factory: creates a FixedTagger that merges system tags
        with the user tags for the given channel_label.

        If channel_label is None or the user tag file doesn't exist, returns
        a tagger with system tags only.
        """
        user_tags_path: Optional[Path] = None
        if channel_label:
            candidate = USER_TAGS_DIR / f"{channel_label}.yaml"
            if candidate.exists():
                user_tags_path = candidate
        return cls(config_path=config_path, user_tags_path=user_tags_path,
                   reload_interval=reload_interval)

    def _load(self) -> None:
        """Load or reload tags from YAML (system + optional user tags)."""
        if not YAML_AVAILABLE:
            raise ImportError("pyyaml required for FixedTagger: pip install pyyaml")

        with self._lock:
            try:
                sys_mtime = self._path.stat().st_mtime
                user_mtime: float = 0.0
                if self._user_tags_path and self._user_tags_path.exists():
                    user_mtime = self._user_tags_path.stat().st_mtime

                if sys_mtime == self._mtime and user_mtime == self._user_mtime:
                    return  # no change

                # Load system tags
                with self._path.open() as f:
                    sys_data = yaml.safe_load(f)
                sys_specs = _parse_tag_specs(sys_data)

                # Load user tags (if path provided)
                user_specs: List[TagSpec] = []
                if self._user_tags_path and self._user_tags_path.exists():
                    with self._user_tags_path.open() as f:
                        user_data = yaml.safe_load(f)
                    user_specs = _parse_tag_specs(user_data or {})

                # Merge: user tags override system tags on name collision
                merged: Dict[str, TagSpec] = {}
                for spec in sys_specs:
                    merged[spec.name] = spec
                for spec in user_specs:
                    merged[spec.name] = spec  # override

                self._tags = list(merged.values())
                self._mtime = sys_mtime
                self._user_mtime = user_mtime

            except Exception as e:
                # On reload failure, keep existing tags
                if not self._tags:
                    raise RuntimeError(f"Failed to load tags from {self._path}: {e}")

    def _maybe_reload(self) -> None:
        """Check if configs have changed and reload if needed."""
        try:
            sys_mtime = self._path.stat().st_mtime
            user_mtime: float = 0.0
            if self._user_tags_path and self._user_tags_path.exists():
                user_mtime = self._user_tags_path.stat().st_mtime
            if sys_mtime != self._mtime or user_mtime != self._user_mtime:
                self._load()
        except OSError:
            pass

    def assign(self, features: MessageFeatures,
               user_text: str, assistant_text: str) -> TagAssignment:
        """Apply fixed rules. Hot-reloads config if changed."""
        self._maybe_reload()

        user_text = _strip_metadata(user_text)
        assistant_text = _strip_metadata(assistant_text)
        combined = (user_text + " " + assistant_text).lower()

        fired_tags = []
        fired_rules = []
        confidences = []

        with self._lock:
            for spec in self._tags:
                matched = self._matches(spec, combined)
                if matched:
                    fired_tags.append(spec.name)
                    fired_rules.append(f"fixed:{spec.name}")
                    confidences.append(spec.confidence)

        avg_conf = sum(confidences) / len(confidences) if confidences else 0.0
        return TagAssignment(
            tags=sorted(fired_tags),
            confidence=avg_conf,
            rules_fired=fired_rules,
        )

    def _matches(self, spec: TagSpec, combined: str) -> bool:
        hits = []
        # Keyword matching (word-boundary)
        for kw in spec.keywords:
            pattern = r"\b" + re.escape(kw) + r"\b"
            if re.search(pattern, combined):
                hits.append(True)
                if not spec.requires_all:
                    break  # Short-circuit for OR logic
        # Pattern matching (only if needed)
        if not (hits and not spec.requires_all):
            for pat in spec.patterns:
                if pat.search(combined):
                    hits.append(True)
                    if not spec.requires_all:
                        break

        if spec.requires_all:
            expected = len(spec.keywords) + len(spec.patterns)
            return len(hits) >= expected
        return len(hits) > 0

    @property
    def tag_names(self) -> List[str]:
        """Return list of active tag names."""
        with self._lock:
            return [t.name for t in self._tags]
