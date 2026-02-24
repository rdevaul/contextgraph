"""
assembler.py — Context assembly policy for the tag-context system.

Builds a context window from a combination of recent messages (recency layer)
and tag-retrieved messages (topic layer), packed to a token budget.
"""

from dataclasses import dataclass
from typing import List

from store import Message, MessageStore


def _estimate_tokens(msg: Message) -> int:
    """Estimate tokens for a message (use stored count or word-count proxy)."""
    if msg.token_count > 0:
        return msg.token_count
    words = len((msg.user_text + " " + msg.assistant_text).split())
    return max(1, int(words * 1.3))


@dataclass
class AssemblyResult:
    """Result of a context assembly operation."""
    messages: List[Message]     # oldest-first, ready to use as context
    total_tokens: int
    recency_count: int          # how many came from the recency layer
    topic_count: int            # how many came from the topic layer
    tags_used: List[str]        # tags that contributed to topic layer


class ContextAssembler:
    """
    Assembles context for an incoming message from two layers:

    1. Recency layer  (25% of budget) — most recent messages regardless of tag
    2. Topic layer    (75% of budget) — messages retrieved by inferred tags,
                                        deduplicated against recency layer

    Final result is sorted oldest-first for natural reading order.
    """

    def __init__(self, store: MessageStore, token_budget: int = 4000) -> None:
        self.store = store
        self.token_budget = token_budget

    def assemble(self, incoming_text: str,
                 inferred_tags: List[str]) -> AssemblyResult:
        """
        Build a context window for `incoming_text` given `inferred_tags`.

        Parameters
        ----------
        incoming_text : str
            The user's new message (used only for future tag inference hooks).
        inferred_tags : List[str]
            Tags inferred for the incoming message by the tagger.
        """
        recency_budget = int(self.token_budget * 0.25)
        topic_budget   = self.token_budget - recency_budget

        # ── Recency layer ──────────────────────────────────────────────────
        recency_msgs: List[Message] = []
        recency_tokens = 0

        for msg in self.store.get_recent(10):
            cost = _estimate_tokens(msg)
            if recency_tokens + cost > recency_budget:
                break
            recency_msgs.append(msg)
            recency_tokens += cost

        # ── Topic layer ────────────────────────────────────────────────────
        seen_ids = {m.id for m in recency_msgs}
        topic_candidates: List[Message] = []

        for tag in inferred_tags:
            for msg in self.store.get_by_tag(tag, limit=20):
                if msg.id not in seen_ids:
                    topic_candidates.append(msg)
                    seen_ids.add(msg.id)

        # newest-first within topic candidates, then pack to budget
        topic_candidates.sort(key=lambda m: m.timestamp, reverse=True)

        topic_msgs: List[Message] = []
        topic_tokens = 0

        for msg in topic_candidates:
            cost = _estimate_tokens(msg)
            if topic_tokens + cost > topic_budget:
                break
            topic_msgs.append(msg)
            topic_tokens += cost

        # ── Combine + sort oldest-first ────────────────────────────────────
        all_msgs = recency_msgs + topic_msgs
        all_msgs.sort(key=lambda m: m.timestamp)

        return AssemblyResult(
            messages=all_msgs,
            total_tokens=recency_tokens + topic_tokens,
            recency_count=len(recency_msgs),
            topic_count=len(topic_msgs),
            tags_used=list(inferred_tags),
        )
