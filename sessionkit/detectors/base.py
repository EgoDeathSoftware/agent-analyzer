"""The shape every detector returns."""

from __future__ import annotations

from dataclasses import dataclass

from sessionkit.parse import Message, ParsedSession

#: Cap on the line numbers a finding cites, so one huge loop doesn't flood a report.
MAX_LINES = 8


@dataclass
class Anomaly:
    """One detected problem in a session."""

    kind: str
    detail: str
    count: int
    lines: list[int]


def assistant_turns(session: ParsedSession) -> list[list[Message]]:
    """Assistant records grouped into whole model messages, in transcript order.

    Claude Code writes one record per content block, all sharing ``msg_id`` and all carrying
    the message's final stop reason; a record with no ``msg_id`` is a message on its own.
    """
    groups: dict[str, list[Message]] = {}
    turns: list[list[Message]] = []
    for message in session.messages:
        if message.role != "assistant":
            continue
        if not message.msg_id:
            turns.append([message])
        elif message.msg_id in groups:
            groups[message.msg_id].append(message)
        else:
            groups[message.msg_id] = [message]
            turns.append(groups[message.msg_id])
    return turns
