"""
tag_registry.py — Hybrid tag lifecycle system with salience-driven promotion/demotion.

Tags flow through three states:
- core: actively matched tags
- candidate: discovered tags being tracked for promotion
- archived: stale tags that are recognized but not actively matched

Salience = f(frequency, recency, distinctiveness) determines promotion/demotion.
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
    state: str  # "core", "candidate", "archived"
    first_seen: float
    last_seen: float
    hits: int
    promoted_at: Optional[float] = None
    archived_at: Optional[float] = None
    # Salience components
    frequency: float = 0.0  # hits per day
    recency_weight: float = 0.0  # exponential decay from last_seen
    distinctiveness: float = 0.0  # inverse document frequency-like score


@dataclass
class TagRegistry:
    """
    Manages the hybrid tag lifecycle: discovery, promotion, demotion, persistence.

    Discovery: Logs dropped tags and extracted entities as candidates.
    Promotion: Candidates with sufficient salience become core tags.
    Demotion: Stale core tags move to archived.
    Persistence: State saved to tag_registry.json.
    """

    data_dir: Path = field(default_factory=lambda: Path(__file__).parent / "data")
    registry_file: str = "tag_registry.json"

    # Promotion thresholds
    min_hits_for_promotion: int = 5
    min_days_for_promotion: int = 3
    min_salience_for_promotion: float = 0.3

    # Demotion thresholds
    stale_days: int = 30

    # Salience weights
    frequency_weight: float = 0.2
    recency_weight: float = 0.3
    distinctiveness_weight: float = 0.5

    # Internal state
    _tags: Dict[str, TagMetadata] = field(default_factory=dict)
    _message_count: int = 0  # total messages processed (for distinctiveness)

    def __post_init__(self):
        """Load registry from disk on init."""
        self.load()

    def load(self) -> None:
        """Load tag registry from JSON file."""
        path = self.data_dir / self.registry_file
        if not path.exists():
            # Bootstrap with initial core tags from tagger.py
            self._bootstrap_core_tags()
            return

        try:
            with open(path, 'r') as f:
                data = json.load(f)
                self._message_count = data.get('message_count', 0)
                for tag_data in data.get('tags', []):
                    tag = TagMetadata(**tag_data)
                    self._tags[tag.name] = tag
        except Exception as e:
            print(f"Error loading tag registry: {e}")
            self._bootstrap_core_tags()

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
                    'frequency': tag.frequency,
                    'recency_weight': tag.recency_weight,
                    'distinctiveness': tag.distinctiveness,
                }
                for tag in self._tags.values()
            ]
        }

        with open(path, 'w') as f:
            json.dump(data, f, indent=2)

    def _bootstrap_core_tags(self) -> None:
        """Bootstrap registry with initial core tags from tagger.py."""
        from tagger import CORE_TAGS

        now = time.time()
        for tag_name in CORE_TAGS:
            self._tags[tag_name] = TagMetadata(
                name=tag_name,
                state="core",
                first_seen=now,
                last_seen=now,
                hits=0,
                promoted_at=now,
            )
        self.save()

    def get_active_tags(self) -> Set[str]:
        """Return set of tags that should be actively matched (core + candidate)."""
        return {
            tag.name for tag in self._tags.values()
            if tag.state in ("core", "candidate")
        }

    def get_core_tags(self) -> Set[str]:
        """Return set of core tags only."""
        return {
            tag.name for tag in self._tags.values()
            if tag.state == "core"
        }

    def get_candidates(self) -> Dict[str, TagMetadata]:
        """Return candidate tags."""
        return {
            name: tag for name, tag in self._tags.items()
            if tag.state == "candidate"
        }

    def get_archived(self) -> Dict[str, TagMetadata]:
        """Return archived tags."""
        return {
            name: tag for name, tag in self._tags.items()
            if tag.state == "archived"
        }

    def discover(self, fired_tags: List[str], dropped_tags: List[str],
                 entities: List[str]) -> None:
        """
        Log candidate tags from:
        - dropped_tags: tags that would have fired but were filtered out
        - entities: proper nouns/project names extracted from message

        Also update hit counts for fired tags.
        """
        now = time.time()
        self._message_count += 1

        # Update fired tags
        for tag_name in fired_tags:
            if tag_name in self._tags:
                tag = self._tags[tag_name]
                tag.last_seen = now
                tag.hits += 1
                self._update_salience(tag)

        # Discover new candidates from dropped tags
        for tag_name in dropped_tags:
            if tag_name not in self._tags:
                self._tags[tag_name] = TagMetadata(
                    name=tag_name,
                    state="candidate",
                    first_seen=now,
                    last_seen=now,
                    hits=1,
                )
                self._update_salience(self._tags[tag_name])
            elif self._tags[tag_name].state == "candidate":
                # Existing candidate, update it
                tag = self._tags[tag_name]
                tag.last_seen = now
                tag.hits += 1
                self._update_salience(tag)

        # Discover new candidates from entities
        # Filter to reasonable tag-like names (alphanumeric, dashes, underscores)
        for entity in entities:
            tag_name = self._normalize_entity_to_tag(entity)
            if tag_name and tag_name not in self._tags:
                self._tags[tag_name] = TagMetadata(
                    name=tag_name,
                    state="candidate",
                    first_seen=now,
                    last_seen=now,
                    hits=1,
                )
                self._update_salience(self._tags[tag_name])

        self.save()

    def _normalize_entity_to_tag(self, entity: str) -> Optional[str]:
        """
        Normalize entity to tag name (lowercase, dashes, no spaces).
        Return None if entity is not suitable for a tag.
        """
        # Convert to lowercase, replace spaces with dashes
        tag = entity.lower().strip()
        tag = tag.replace(' ', '-')

        # Filter out non-tag-like entities
        if len(tag) < 3:  # too short
            return None
        if len(tag) > 30:  # too long
            return None
        if not tag.replace('-', '').replace('_', '').isalnum():  # contains weird chars
            return None

        return tag

    def _update_salience(self, tag: TagMetadata) -> None:
        """Update salience score for a tag based on frequency, recency, distinctiveness."""
        now = time.time()

        # Frequency: hits per day since first seen
        days_since_first = max(1, (now - tag.first_seen) / 86400)
        tag.frequency = tag.hits / days_since_first

        # Recency: exponential decay from last_seen (half-life = 7 days)
        days_since_last = (now - tag.last_seen) / 86400
        tag.recency_weight = 2 ** (-days_since_last / 7)

        # Distinctiveness: inverse document frequency-like score
        # Higher if tag doesn't appear in every message
        if self._message_count > 0:
            tag.distinctiveness = 1.0 - (tag.hits / self._message_count)
        else:
            tag.distinctiveness = 0.0

    def salience(self, tag_name: str) -> float:
        """Calculate salience score for a tag (0.0 to 1.0)."""
        if tag_name not in self._tags:
            return 0.0

        tag = self._tags[tag_name]
        self._update_salience(tag)

        # Normalize frequency to 0-1 range (cap at 1.0 hit/day = max)
        norm_freq = min(1.0, tag.frequency)

        # Weighted combination
        salience = (
            self.frequency_weight * norm_freq +
            self.recency_weight * tag.recency_weight +
            self.distinctiveness_weight * tag.distinctiveness
        )

        return salience

    def promote_candidates(self) -> List[str]:
        """
        Check all candidates for promotion to core.
        Returns list of newly promoted tag names.
        """
        promoted = []
        now = time.time()

        for tag_name, tag in list(self._tags.items()):
            if tag.state != "candidate":
                continue

            # Check promotion criteria
            days_active = (now - tag.first_seen) / 86400
            salience_score = self.salience(tag_name)

            if (tag.hits >= self.min_hits_for_promotion and
                days_active >= self.min_days_for_promotion and
                salience_score >= self.min_salience_for_promotion):

                tag.state = "core"
                tag.promoted_at = now
                promoted.append(tag_name)

        if promoted:
            self.save()

        return promoted

    def demote_stale(self) -> List[str]:
        """
        Move stale core tags to archived.
        Returns list of newly archived tag names.
        """
        archived = []
        now = time.time()

        for tag_name, tag in list(self._tags.items()):
            if tag.state != "core":
                continue

            days_since_last = (now - tag.last_seen) / 86400

            if days_since_last >= self.stale_days:
                tag.state = "archived"
                tag.archived_at = now
                archived.append(tag_name)

        if archived:
            self.save()

        return archived

    def force_promote(self, tag_name: str) -> bool:
        """Force-promote a candidate to core. Returns True if successful."""
        if tag_name not in self._tags:
            return False

        tag = self._tags[tag_name]
        if tag.state != "candidate":
            return False

        tag.state = "core"
        tag.promoted_at = time.time()
        self.save()
        return True

    def force_demote(self, tag_name: str) -> bool:
        """Force-archive a core tag. Returns True if successful."""
        if tag_name not in self._tags:
            return False

        tag = self._tags[tag_name]
        if tag.state != "core":
            return False

        tag.state = "archived"
        tag.archived_at = time.time()
        self.save()
        return True

    def get_all_tags(self) -> Dict[str, Dict]:
        """Return all tags with full metadata (for API/dashboard)."""
        result = {
            'core': [],
            'candidate': [],
            'archived': [],
        }

        for tag_name, tag in self._tags.items():
            tag_dict = {
                'name': tag.name,
                'hits': tag.hits,
                'salience': self.salience(tag_name),
                'last_seen': tag.last_seen,
                'first_seen': tag.first_seen,
                'promoted_at': tag.promoted_at,
                'archived_at': tag.archived_at,
            }
            result[tag.state].append(tag_dict)

        # Sort by salience (descending)
        for state in result:
            result[state].sort(key=lambda x: x['salience'], reverse=True)

        return result


# Global singleton instance
_registry_instance = None

def get_registry() -> TagRegistry:
    """Get the global TagRegistry singleton."""
    global _registry_instance
    if _registry_instance is None:
        _registry_instance = TagRegistry()
    return _registry_instance
