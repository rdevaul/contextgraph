"""Channel access rules for per-agent memory assembly."""

AGENT_CHANNEL_ACCESS = {
    "main": ["rich-dm", "rich-household"],
    "glados-rich": ["rich-dm", "rich-household"],
    "glados-household": ["rich-household"],
    "glados-dana": ["dana-dm", "rich-household"],
    "glados-terry": ["terry-dm", "rich-household"],
    "glados-lily": ["lily-dm"],
    "glados-lynae": ["lynae-dm"],
}


def get_allowed_labels(agent_id: str) -> list:
    """Return the list of channel labels accessible to the given agent."""
    return AGENT_CHANNEL_ACCESS.get(agent_id, [])


def filter_turns_for_agent(turns: list, agent_id: str) -> list:
    """Filter turns to only those visible to the given agent. Unlabeled turns are excluded."""
    allowed = set(get_allowed_labels(agent_id))
    return [t for t in turns if t.get("channel_label") in allowed]
