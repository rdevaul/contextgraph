"""
sticky.py — Sticky pin manager for context assembly.

Manages a third "sticky" layer that pins active tool call chains and explicit
work-in-progress into the context window, preventing the agent from losing track
of ongoing work across turns.
"""

import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


@dataclass
class StickyPin:
    """A pin that keeps specific messages in the context window."""
    pin_id: str                     # Unique identifier
    message_ids: List[str]          # Messages in this pin group
    pin_type: str                   # "tool_chain" | "explicit" | "reference"
    created_at: float               # Timestamp when pinned
    ttl_turns: int                  # Turns until auto-unpin
    turns_elapsed: int              # Turns since pinned
    total_tokens: int               # Token cost of this pin group
    reason: str                     # Why this was pinned

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "StickyPin":
        """Create from dict for JSON deserialization."""
        return cls(**data)


class StickyPinManager:
    """
    Manages sticky pins for the context assembly system.

    Pins are stored in memory with JSON backup at ~/.tag-context/sticky-state.json.
    Maximum 5 active pins; LRU eviction beyond that.
    Auto-expires pins when turns_elapsed >= ttl_turns.
    """

    DEFAULT_STATE_PATH = Path.home() / ".tag-context" / "sticky-state.json"
    MAX_ACTIVE_PINS = 5

    def __init__(self, state_path: Optional[str] = None) -> None:
        self.state_path = Path(state_path) if state_path else self.DEFAULT_STATE_PATH
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.pins: List[StickyPin] = []
        self._load_state()

    # ── State persistence ──────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load pins from JSON backup file."""
        if not self.state_path.exists():
            return

        try:
            with open(self.state_path, 'r') as f:
                data = json.load(f)
                self.pins = [StickyPin.from_dict(p) for p in data.get("active_pins", [])]
        except Exception as e:
            # If state file is corrupted, start fresh
            print(f"Warning: Could not load sticky state from {self.state_path}: {e}")
            self.pins = []

    def _save_state(self) -> None:
        """Save pins to JSON backup file."""
        try:
            data = {
                "active_pins": [p.to_dict() for p in self.pins],
                "total_sticky_tokens": sum(p.total_tokens for p in self.pins),
            }
            with open(self.state_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save sticky state to {self.state_path}: {e}")

    # ── Pin management ─────────────────────────────────────────────────────

    def add_pin(self, message_ids: List[str], pin_type: str,
                reason: str, ttl_turns: int, total_tokens: int) -> str:
        """
        Create a new pin.

        Parameters
        ----------
        message_ids : List[str]
            Message IDs to pin
        pin_type : str
            "tool_chain" | "explicit" | "reference"
        reason : str
            Human-readable reason for pinning
        ttl_turns : int
            Turns until auto-expiry
        total_tokens : int
            Token cost of the pinned messages

        Returns
        -------
        str
            The pin_id of the newly created pin
        """
        # If we're at capacity, evict the oldest pin (LRU)
        if len(self.pins) >= self.MAX_ACTIVE_PINS:
            self._evict_oldest()

        pin_id = f"{pin_type[:2]}-{int(time.time())}-{str(uuid.uuid4())[:8]}"
        pin = StickyPin(
            pin_id=pin_id,
            message_ids=message_ids,
            pin_type=pin_type,
            created_at=time.time(),
            ttl_turns=ttl_turns,
            turns_elapsed=0,
            total_tokens=total_tokens,
            reason=reason,
        )
        self.pins.append(pin)
        self._save_state()
        return pin_id

    def remove_pin(self, pin_id: str) -> bool:
        """
        Remove a pin by ID.

        Returns
        -------
        bool
            True if pin was found and removed, False otherwise
        """
        original_len = len(self.pins)
        self.pins = [p for p in self.pins if p.pin_id != pin_id]
        removed = len(self.pins) < original_len

        if removed:
            self._save_state()

        return removed

    def get_active_pins(self) -> List[StickyPin]:
        """Return all active pins."""
        return list(self.pins)

    def get_pin_by_id(self, pin_id: str) -> Optional[StickyPin]:
        """Get a specific pin by ID, or None if not found."""
        for pin in self.pins:
            if pin.pin_id == pin_id:
                return pin
        return None

    def get_pinned_message_ids(self) -> List[str]:
        """Return all message IDs that are currently pinned (deduplicated)."""
        message_ids = []
        seen = set()
        for pin in self.pins:
            for msg_id in pin.message_ids:
                if msg_id not in seen:
                    message_ids.append(msg_id)
                    seen.add(msg_id)
        return message_ids

    def tick(self) -> List[str]:
        """
        Increment turns_elapsed for all pins and expire stale ones.

        Called on each /assemble request.

        Returns
        -------
        List[str]
            Pin IDs that were expired
        """
        expired = []

        for pin in self.pins:
            pin.turns_elapsed += 1
            if pin.turns_elapsed > pin.ttl_turns:
                expired.append(pin.pin_id)

        # Remove expired pins
        if expired:
            self.pins = [p for p in self.pins if p.pin_id not in expired]
            self._save_state()

        return expired

    def update_or_create_tool_chain_pin(self, message_ids: List[str],
                                        reason: str, total_tokens: int,
                                        ttl_turns: int = 10) -> str:
        """
        Update existing tool_chain pin or create new one.

        If a tool_chain pin already exists, extend it with new message IDs
        and reset its TTL. Otherwise, create a new pin.

        Parameters
        ----------
        message_ids : List[str]
            Message IDs in the tool chain
        reason : str
            Human-readable reason
        total_tokens : int
            Token cost of the chain
        ttl_turns : int
            TTL in turns (default: 10)

        Returns
        -------
        str
            The pin_id (existing or new)
        """
        # Look for an existing tool_chain pin
        existing = None
        for pin in self.pins:
            if pin.pin_type == "tool_chain":
                existing = pin
                break

        if existing:
            # Extend the existing pin
            # Add new message IDs that aren't already pinned
            existing_ids = set(existing.message_ids)
            new_ids = [mid for mid in message_ids if mid not in existing_ids]
            existing.message_ids.extend(new_ids)
            existing.total_tokens = total_tokens
            existing.turns_elapsed = 0  # Reset TTL
            existing.ttl_turns = ttl_turns
            existing.reason = reason
            self._save_state()
            return existing.pin_id
        else:
            # Create new pin
            return self.add_pin(
                message_ids=message_ids,
                pin_type="tool_chain",
                reason=reason,
                ttl_turns=ttl_turns,
                total_tokens=total_tokens,
            )

    def _evict_oldest(self) -> None:
        """Evict the oldest pin (LRU policy)."""
        if not self.pins:
            return

        # Sort by created_at and remove the oldest
        self.pins.sort(key=lambda p: p.created_at)
        self.pins.pop(0)
        # _save_state will be called by the caller
