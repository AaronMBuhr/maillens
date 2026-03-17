"""
Thread reconstruction from Message-ID, In-Reply-To, and References headers.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ThreadNode:
    message_id: str
    parent_id: Optional[str] = None
    children: list[str] = field(default_factory=list)
    thread_root: Optional[str] = None


def build_threads(
    messages: list[dict],
) -> dict[str, int]:
    """
    Given a list of message dicts with 'message_id', 'in_reply_to', and 'references',
    group them into threads.

    Returns a mapping of message_id -> thread_group_id (int).
    """
    # Build parent-child relationships
    parent_map: dict[str, str] = {}  # message_id -> parent_message_id

    for msg in messages:
        mid = msg["message_id"]
        in_reply_to = msg.get("in_reply_to")
        references = msg.get("references")

        if in_reply_to:
            parent_map[mid] = in_reply_to
        elif references:
            # References header lists message IDs in order; the last one is the
            # direct parent, the first one is the thread root
            ref_ids = references.strip().split()
            if ref_ids:
                parent_map[mid] = ref_ids[-1]  # direct parent

    # Find root for each message by walking up the chain
    def find_root(mid: str, visited: set[str] | None = None) -> str:
        if visited is None:
            visited = set()
        if mid in visited:
            return mid  # cycle detected
        visited.add(mid)
        parent = parent_map.get(mid)
        if parent and parent != mid:
            return find_root(parent, visited)
        return mid

    # Group messages by root
    root_map: dict[str, str] = {}
    for msg in messages:
        mid = msg["message_id"]
        root = find_root(mid)
        root_map[mid] = root

    # Assign integer thread IDs
    root_to_thread_id: dict[str, int] = {}
    next_id = 0
    thread_assignments: dict[str, int] = {}

    for mid, root in root_map.items():
        if root not in root_to_thread_id:
            root_to_thread_id[root] = next_id
            next_id += 1
        thread_assignments[mid] = root_to_thread_id[root]

    return thread_assignments
