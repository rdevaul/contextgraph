"""
tag_registry.py — Tag definitions from YAML files. Single source of truth.

SYSTEM TAGS: loaded from tags.yaml (package root)
USER TAGS: one YAML file per user in ~/.tag-context/tags.user.registry/

NO system_tags.json. NO JSON tag config. YAML only.
tag_registry.json is purely a runtime statistics overlay (hits, timestamps,
and any runtime-added tags not yet migrated to user YAML).
"""

import json
import time
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Set, Optional


SYSTEM_TAGS_YAML = Path(__file__).parent / "tags.yaml"
USER_REGISTRY_DIR = Path(__file__).parent.parent / ".tag-context" / "tags.user.registry"


@dataclass
class TagConfig:
    """A tag's configuration — YAML is always authoritative."""
    name: str
    description: str = ""
    keywords: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    requires_all: bool = False
    confidence: float = 1.0
    enabled: bool = True
    state: str = "core"


@dataclass
class TagRuntime:
    """Runtime metadata (hits, timestamps). Never overrides YAML config."""
    name: str
    state: str = "core"
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 0
    promoted_at: Optional[float] = None
    archived_at: Optional[float] = None


def _load_yaml(path: Path) -> Dict[str, TagConfig]:
    """Load tag configs from a YAML file."""
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    tags = {}
    for entry in data.get("tags", []):
        name = entry["name"]
        enabled = entry.get("enabled", True)
        state = entry.get("state", "archived" if not enabled else "core")
        tags[name] = TagConfig(
            name=name,
            description=entry.get("description", ""),
            keywords=entry.get("keywords", []),
            patterns=entry.get("patterns", []),
            requires_all=entry.get("requires_all", False),
            confidence=entry.get("confidence", 1.0),
            enabled=enabled,
            state=state,
        )
    return tags


def _save_yaml_tags(path: Path, tags: Dict[str, TagConfig]) -> None:
    """Save tag configs to a YAML file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for tag in sorted(tags.values(), key=lambda t: t.name):
        entry = {"name": tag.name}
        if tag.description:
            entry["description"] = tag.description
        if tag.keywords:
            entry["keywords"] = tag.keywords
        if tag.patterns:
            entry["patterns"] = tag.patterns
        if tag.requires_all:
            entry["requires_all"] = True
        if tag.confidence != 1.0:
            entry["confidence"] = tag.confidence
        if not tag.enabled:
            entry["enabled"] = False
        if tag.state != "core":
            entry["state"] = tag.state
        entries.append(entry)
    with open(path, "w") as f:
        yaml.dump({"tags": entries}, f, default_flow_style=False, sort_keys=False)


class TagRegistry:
    """
    System mode (_is_system=True):
      - Loads tag configs from system YAML (tags.yaml)
      - Loads runtime stats from JSON overlay
      - Runtime-added user tags also loaded from JSON (fallback until migrated)
    
    User mode (_is_system=False):
      - Loads tag configs from user YAML file
      - Loads runtime stats from matching JSON overlay
    
    When user tags are added/removed at runtime, they're saved to the
    user's YAML file so they persist across restarts.
    """
    def __init__(self, data_dir: Path = None, registry_file: str = None,
                 yaml_path: Path = None, is_system: bool = None):
        if data_dir is None:
            data_dir = Path(__file__).parent / "data"
        self.data_dir = data_dir
        self.registry_file = registry_file or "tag_registry.json"
        self.yaml_path = yaml_path or SYSTEM_TAGS_YAML

        if is_system is not None:
            self._is_system = is_system
        else:
            self._is_system = (self.registry_file == "tag_registry.json"
                               and self.yaml_path == SYSTEM_TAGS_YAML)

        self._configs: Dict[str, TagConfig] = {}
        self._runtime: Dict[str, TagRuntime] = {}
        self.message_count: int = 0
        self.load()

    def load(self) -> None:
        """Load YAML configs + runtime metadata overlay."""
        self._configs = _load_yaml(self.yaml_path)
        state_path = self.data_dir / self.registry_file
        if state_path.exists():
            try:
                with open(state_path) as f:
                    data = json.load(f)
                self.message_count = data.get('message_count', 0)
                for item in data.get('tags', []):
                    name = item['name']
                    if name in self._configs:
                        # Update runtime stats for known tag
                        self._runtime[name] = TagRuntime(
                            name=name,
                            state=self._configs[name].state,
                            first_seen=item.get('first_seen', time.time()),
                            last_seen=item.get('last_seen', time.time()),
                            hits=item.get('hits', 0),
                            promoted_at=item.get('promoted_at'),
                            archived_at=item.get('archived_at'),
                        )
                    elif not self._is_system:
                        # Non-system: load runtime-added tags from JSON
                        self._configs[name] = TagConfig(
                            name=name,
                            state=item.get('state', 'core'),
                        )
                        self._runtime[name] = TagRuntime(
                            name=name,
                            state=item.get('state', 'core'),
                            first_seen=item.get('first_seen', time.time()),
                            last_seen=item.get('last_seen', time.time()),
                            hits=item.get('hits', 0),
                            promoted_at=item.get('promoted_at'),
                            archived_at=item.get('archived_at'),
                        )
            except Exception as e:
                print(f"Error loading runtime state: {e}")

    def save(self) -> None:
        """Persist runtime metadata to JSON and configs to YAML."""
        path = self.data_dir / self.registry_file
        path.parent.mkdir(parents=True, exist_ok=True)
        all_tags = {}
        for name, cfg in self._configs.items():
            if name in self._runtime:
                all_tags[name] = self._runtime[name]
            else:
                all_tags[name] = TagRuntime(
                    name=name, state=cfg.state,
                    first_seen=time.time(),
                    last_seen=time.time(), hits=0,
                )
        data = {
            'message_count': self.message_count,
            'tags': [
                {'name': t.name, 'state': t.state,
                 'first_seen': t.first_seen, 'last_seen': t.last_seen,
                 'hits': t.hits, 'promoted_at': t.promoted_at,
                 'archived_at': t.archived_at}
                for t in all_tags.values()
            ]
        }
        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

        # Non-system registries also persist configs to user YAML
        if not self._is_system:
            _save_yaml_tags(self.yaml_path, self._configs)

    def get_active_tags(self) -> Set[str]:
        """Enabled, non-archived tags from YAML configs."""
        return {n for n, c in self._configs.items()
                if c.enabled and c.state != "archived"}

    def get_core_tags(self) -> Set[str]:
        return self.get_active_tags()

    def get_active_tags_for_channel(self, channel_label: Optional[str]) -> Set[str]:
        system_active = get_registry().get_active_tags()
        if not channel_label:
            return system_active
        user_reg = get_user_registry(channel_label)
        return system_active | (user_reg.get_active_tags() if user_reg else set())

    def get_all_tags(self) -> Dict[str, list]:
        result = {'core': [], 'archived': []}
        for name, cfg in self._configs.items():
            rt = self._runtime.get(name)
            tag_dict = {
                'name': name, 'state': cfg.state, 'enabled': cfg.enabled,
                'description': cfg.description, 'keywords': cfg.keywords,
                'hits': rt.hits if rt else 0,
                'last_seen': rt.last_seen if rt else 0,
                'first_seen': rt.first_seen if rt else 0,
            }
            result.setdefault(cfg.state, []).append(tag_dict)
        for s in result:
            result[s].sort(key=lambda x: -x['hits'])
        return result

    def record_hit(self, tag_name: str) -> None:
        if tag_name in self._configs:
            if tag_name not in self._runtime:
                self._runtime[tag_name] = TagRuntime(
                    name=tag_name,
                    state=self._configs[tag_name].state,
                    first_seen=time.time(),
                    last_seen=time.time(), hits=0,
                )
            self._runtime[tag_name].hits += 1
            self._runtime[tag_name].last_seen = time.time()
            if not self._is_system:
                self.save()

    def add_system_tag(self, name: str, state: str = "core") -> bool:
        if name in self._configs:
            return False
        self._configs[name] = TagConfig(name=name, state=state)
        return True

    def remove_system_tag(self, name: str) -> bool:
        if name not in self._configs:
            return False
        del self._configs[name]
        self._runtime.pop(name, None)
        return True

    def add_user_tag(self, name: str, state: str = "core") -> bool:
        if name in self._configs:
            return False
        self._configs[name] = TagConfig(name=name, state=state)
        self._runtime[name] = TagRuntime(
            name=name, state=state,
            first_seen=time.time(),
            last_seen=time.time(), hits=0,
        )
        self.save()
        return True

    def remove_user_tag(self, name: str) -> bool:
        if name not in self._configs:
            return False
        del self._configs[name]
        self._runtime.pop(name, None)
        self.save()
        return True

    def get_tag_def(self, name: str) -> Optional[TagConfig]:
        return self._configs.get(name)

    def get_tag_defs(self) -> Dict[str, TagConfig]:
        return dict(self._configs)


# ── Singletons ────────────────────────────────────────────────────────────────
_registry_instance: Optional['TagRegistry'] = None
_user_registry_cache: Dict[str, 'TagRegistry'] = {}


def get_registry() -> 'TagRegistry':
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = TagRegistry()
    return _registry_instance


def get_user_registry(channel_label: str) -> Optional['TagRegistry']:
    USER_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    if channel_label not in _user_registry_cache:
        user_yaml = USER_REGISTRY_DIR / f"{channel_label}.yaml"
        if not user_yaml.exists():
            return None
        user_reg = TagRegistry(
            registry_file=f"{channel_label}.json",
            is_system=False, yaml_path=user_yaml,
            data_dir=USER_REGISTRY_DIR,
        )
        _user_registry_cache[channel_label] = user_reg
    return _user_registry_cache.get(channel_label)


def clear_user_registry_cache() -> None:
    global _user_registry_cache
    _user_registry_cache = {}


def reload_registry():
    global _registry_instance
    _registry_instance = TagRegistry()
