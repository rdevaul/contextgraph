"""
tag_registry.py — Explicit-only tag system.

System tags are loaded from data/system_tags.json on startup.
User tags are only added via explicit /tags command.
No auto-discovery, no auto-promotion, no auto-demotion.

Design doc: docs/TAG_SYSTEM_DESIGN.md
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Optional


@dataclass
class TagMetadata:
    """Metadata for a single tag in the registry."""
    name: str
    state: str  # "core", "archived"
    first_seen: float
    last_seen: float
    hits: int
    promoted_at: Optional[float] = None
    archived_at: Optional[float] = None


# Directory for per-user tag registries
USER_REGISTRY_DIR = Path.home() / ".tag-context" / "tags.user.registry"


class TagRegistry:
    """
    Explicit-only tag registry.

    System tags are loaded from a static config file (system_tags.json).
    User registries track per-user tag state, modified only by
    explicit /tags commands.

    No auto-discovery, no auto-promotion, no auto-demotion.
    """

    def __init__(self, data_dir: Path = None, registry_file: str = None,
                 system_config_path: Path = None):
        """
        Args:
            data_dir: Base directory for tag data files.
            registry_file: Filename for this registry (system or user).
            system_config_path: Path to system_tags.json (system registries only).
                If None, defaults to data_dir/system_tags.json when
                registry_file is "tag_registry.json" (i.e. system mode).
        """
        if data_dir is None:
            data_dir = Path(__file__).parent / "data"
        self.data_dir = data_dir
        self.registry_file = registry_file or "tag_registry.json"
        self.system_config_path = system_config_path or (data_dir / "system_tags.json")

        self._tags: Dict[str, TagMetadata] = {}
        self._message_count: int = 0
        # System registry is identified by file name AND explicit config path.
        # User registries have their own file and do NOT load system config.
        # Only the canonical system file name + presence of config = system mode.
        self._is_system = (
            self.registry_file == "tag_registry.json"
            and self.system_config_path.exists()
        )
        self.load()

    def load(self) -> None:
        """Load tag registry from disk.

        For the system registry: load from system_tags.json config file.
        For user registries: load from the user-specific JSON file.
        """
        if self._is_system:
            # Load system tags from explicit config file
            if self.system_config_path.exists():
                try:
                    with open(self.system_config_path, 'r') as f:
                        data = json.load(f)
                        for tag_entry in data.get('tags', []):
                            name = tag_entry['name']
                            state = tag_entry.get('state', 'core')
                            self._tags[name] = TagMetadata(
                                name=name,
                                state=state,
                                first_seen=time.time(),
                                last_seen=time.time(),
                                hits=0,
                                promoted_at=time.time() if state == "core" else None,
                            )
                except Exception as e:
                    print(f"Error loading system tags from {self.system_config_path}: {e}")
        else:
            # Load user registry from its JSON file
            path = self.data_dir / self.registry_file
            if path.exists():
                try:
                    with open(path, 'r') as f:
                        data = json.load(f)
                        self._message_count = data.get('message_count', 0)
                        for tag_data in data.get('tags', []):
                            tag = TagMetadata(**tag_data)
                            self._tags[tag.name] = tag
                except Exception as e:
                    print(f"Error loading user registry {path}: {e}")

    def save(self) -> None:
        """Save tag registry to JSON file."""
        path = self.data_dir / self.registry_file
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            'message_count': self._message_count,
            'tags': [
                {
                    'name': tag.name,
                    'state': tag.state,
                    'first_seen': tag.first_seen,
                    'last_seen': tag.last_seen,
                    'hits': tag.hits,
                    'promoted_at': tag.promoted_at,
                    'archived_at': tag.archived_at,
                }
                for tag in self._tags.values()
            ]
        }

        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    # ---- System tag management (explicit only) ----

    def add_system_tag(self, name: str, state: str = "core") -> bool:
        """Add a system tag. Returns True if it was new, False if already exists."""
        if name in self._tags:
            return False
        now = time.time()
        self._tags[name] = TagMetadata(
            name=name,
            state=state,
            first_seen=now,
            last_seen=now,
            hits=0,
        )
        return True

    def remove_system_tag(self, name: str) -> bool:
        """Remove a system tag. Returns True if it existed."""
        if name not in self._tags:
            return False
        del self._tags[name]
        return True

    # ---- User tag management (explicit only) ----

    def add_user_tag(self, name: str, state: str = "core") -> bool:
        """Add a user tag. Returns True if it was new, False if already exists."""
        if name in self._tags:
            return False
        now = time.time()
        self._tags[name] = TagMetadata(
            name=name,
            state=state,
            first_seen=now,
            last_seen=now,
            hits=0,
        )
        self.save()
        return True

    def remove_user_tag(self, name: str) -> bool:
        """Remove a user tag. Returns True if it existed."""
        if name not in self._tags:
            return False
        del self._tags[name]
        self.save()
        return True

    # ---- Query methods ----

    def get_active_tags(self) -> Set[str]:
        """Return set of tags that should be actively matched (core only)."""
        return {
            tag.name for tag in self._tags.values()
            if tag.state == "core"
        }

    def get_active_tags_for_channel(self, channel_label: Optional[str]) -> Set[str]:
        """
        Return combined active tags for a channel: system active + user active.
        """
        system_active = get_registry().get_active_tags()
        if not channel_label:
            return system_active

        user_reg = get_user_registry(channel_label)
        if user_reg is None:
            return system_active

        return system_active | user_reg.get_active_tags()

    def get_core_tags(self) -> Set[str]:
        """Return set of core tags only."""
        return {
            tag.name for tag in self._tags.values()
            if tag.state == "core"
        }

    def get_all_tags(self) -> Dict[str, List[Dict]]:
        """Return all tags grouped by state (for API/dashboard)."""
        result = {
            'core': [],
            'archived': [],
        }
        for tag_name, tag in self._tags.items():
            tag_dict = {
                'name': tag.name,
                'state': tag.state,
                'hits': tag.hits,
                'last_seen': tag.last_seen,
                'first_seen': tag.first_seen,
                'promoted_at': tag.promoted_at,
                'archived_at': tag.archived_at,
            }
            result.setdefault(tag.state, []).append(tag_dict)

        # Sort by hits descending within each state
        for state in result:
            result[state].sort(key=lambda x: -x['hits'])

        return result

    def record_hit(self, tag_name: str) -> None:
        """Record a match for a tag (for tracking user tag adoption)."""
        if tag_name in self._tags:
            self._tags[tag_name].hits += 1
            self._tags[tag_name].last_seen = time.time()
            # Only save for user registries (system registry doesn't persist hits)
            if not self._is_system:
                self.save()

    @property
    def _tags(self):
        return self.__dict__.get('_tags', {})

    @_tags.setter
    def _tags(self, value):
        self.__dict__['_tags'] = value

    @property
    def _message_count(self):
        return self.__dict__.get('_message_count', 0)

    @_message_count.setter
    def _message_count(self, value):
        self.__dict__['_message_count'] = value


# Global singleton instance
_registry_instance = None


def get_registry() -> TagRegistry:
    """Get the global TagRegistry singleton."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = TagRegistry()
    return _registry_instance


# Per-user registry cache
_user_registry_cache: Dict[str, TagRegistry] = {}


def get_user_registry(channel_label: str) -> Optional[TagRegistry]:
    """
    Get (or create) the TagRegistry for a specific user channel.

    User registries are stored at ~/.tag-context/tags.user.registry/<label>.json.
    Returns None if the user registry directory doesn't exist yet.
    """
    USER_REGISTRY_DIR.mkdir(parents=True, exist_ok=True)

    if channel_label not in _user_registry_cache:
        registry_path = USER_REGISTRY_DIR / f"{channel_label}.json"
        if not registry_path.exists():
            # Don't create empty user registries — only load existing ones
            return _registry_instance  # Actually, return None to indicate no user reg
        user_reg = TagRegistry(
            data_dir=USER_REGISTRY_DIR,
            registry_file=f"{channel_label}.json",
        )
        _user_registry_cache[channel_label] = user_reg

    return _user_registry_cache.get(channel_label)


def clear_user_registry_cache() -> None:
    """Clear the user registry cache (useful for testing)."""
    global _user_registry_cache
    _user_registry_cache = {}
