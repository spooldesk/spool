"""Who else is looking at this ticket, and who is typing into it.

Kept in process memory rather than the database. The whole record is worth a
few seconds — writing "Ivan opened ticket 42" to disk twelve times a minute per
operator would be the most-written table in the product and the least-read.

The one thing to know before deploying: this is per-process. With several
workers, operators on different workers will not see each other. Fine on a
single-worker instance, which is what the container-per-tenant setup runs; if
that ever changes, this module is the thing that needs Redis behind it, and
nothing else has to move.

Telegram is not involved. A bot cannot be told that a customer is typing — that
event never reaches it — so this is strictly operator-to-operator.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# How long a heartbeat counts for. The client beats every 5s, so 15 tolerates
# two lost beats before an operator is declared gone — long enough to survive a
# hiccup, short enough that a closed tab clears quickly.
PRESENCE_TTL_SEC = 15.0

# Typing fades faster than presence: it describes what someone is doing right
# now, and a stale "typing…" is worse than none.
TYPING_TTL_SEC = 6.0


@dataclass(slots=True)
class Viewer:
    operator_id: int
    name: str
    seen_at: float
    typing_until: float = 0.0

    def is_typing(self, now: float) -> bool:
        return self.typing_until > now


# ticket_id -> operator_id -> Viewer
_viewers: dict[int, dict[int, Viewer]] = {}


def touch(ticket_id: int, operator_id: int, name: str, *, typing: bool = False) -> None:
    """Record that this operator is on this ticket, optionally typing."""
    now = time.monotonic()
    room = _viewers.setdefault(ticket_id, {})
    viewer = room.get(operator_id)
    if viewer is None:
        viewer = Viewer(operator_id=operator_id, name=name, seen_at=now)
        room[operator_id] = viewer
    viewer.name = name
    viewer.seen_at = now
    if typing:
        viewer.typing_until = now + TYPING_TTL_SEC


def leave(ticket_id: int, operator_id: int) -> None:
    """Drop an operator immediately, when their tab tells us it is closing."""
    room = _viewers.get(ticket_id)
    if room:
        room.pop(operator_id, None)
        if not room:
            _viewers.pop(ticket_id, None)


def others(ticket_id: int, operator_id: int) -> list[dict]:
    """Everyone except the caller who is currently on this ticket.

    Expiry happens here rather than on a timer: presence is only ever read to
    render it, so there is nothing to clean up between reads.
    """
    now = time.monotonic()
    room = _viewers.get(ticket_id)
    if not room:
        return []

    for oid, viewer in list(room.items()):
        if now - viewer.seen_at > PRESENCE_TTL_SEC:
            del room[oid]
    if not room:
        _viewers.pop(ticket_id, None)
        return []

    return [
        {"id": v.operator_id, "name": v.name, "typing": v.is_typing(now)}
        for v in room.values()
        if v.operator_id != operator_id
    ]


def reset() -> None:
    """Drop everything. For tests."""
    _viewers.clear()
