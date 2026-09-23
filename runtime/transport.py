"""The one interface the runtime needs from whatever carries the conversation.

Keeping this narrow is what makes the rest of the runtime domain-free. In the system this
was extracted from, the transport was a LinkedIn outreach platform's REST API; it could as
easily be a helpdesk, an SMS gateway or an inbox. Everything above this line - debounce,
claim, draft, approve, send, learn - is identical either way.

THE GATE THAT BELONGS HERE, NOT IN A PROMPT. `should_reply` answers "is it even our turn?"
and it is deterministic code, not a model call. It began life as an implicit assumption
inside a prompt, which meant the system would occasionally draft a reply to its own last
message. A rule that a prompt keeps breaking belongs in code; this is one of the four that
made that journey.
"""
from dataclasses import dataclass
from typing import List, Protocol


@dataclass(frozen=True)
class Message:
    """One message in a conversation. `inbound` means it came FROM the other party."""
    id: str
    text: str
    sent_at: str          # ISO 8601, UTC
    inbound: bool


class Transport(Protocol):
    """Implement these two and the runtime works against your channel."""

    def fetch_thread(self, subject_id: str) -> List[Message]:
        """The full conversation with one subject, in any order."""

    def send(self, subject_id: str, text: str) -> str:
        """Deliver a message. Returns a provider id. Raises on failure - the caller
        treats an exception as "not sent" and leaves the approval open."""


def ordered(messages: List[Message]) -> List[Message]:
    """Sorted by time, with the unsendable dropped.

    Ordering by a timestamp the provider supplies, not by the order the API returned them:
    paged endpoints hand back newest-first, and a thread read in that order inverts every
    "who spoke last" answer downstream.
    """
    return sorted([m for m in messages if (m.text or "").strip()], key=lambda m: m.sent_at or "")


def should_reply(messages: List[Message]) -> bool:
    """Only when the other party has the last word.

    Everything else - a delivery receipt, our own message echoed back by the provider, a
    webhook firing twice - is not our turn. This single check removed a whole class of
    "why did it answer itself" incidents.
    """
    msgs = ordered(messages)
    return bool(msgs) and msgs[-1].inbound


def render(messages: List[Message]) -> str:
    """The conversation as the model sees it.

    One fixed format, in one place. When a second engine rendered threads its own way, the
    two drifted and a prompt tuned against one shape quietly degraded on the other.
    """
    lines = []
    for m in ordered(messages):
        who = "<- THEM" if m.inbound else "-> US"
        lines.append(f"[{(m.sent_at or '')[:19].replace('T', ' ')}] {who}\n{m.text.strip()}\n")
    return "\n".join(lines).strip()


class MemoryTransport:
    """An in-process transport, so the example runs with no network and tests have no
    fixtures to keep in sync. Sends are recorded, never delivered anywhere."""

    def __init__(self, threads=None):
        self.threads = dict(threads or {})
        self.sent = []

    def fetch_thread(self, subject_id):
        return list(self.threads.get(subject_id, []))

    def send(self, subject_id, text):
        self.sent.append((subject_id, text))
        msg = Message(id=f"out-{len(self.sent)}", text=text,
                      sent_at="2026-01-01T00:00:00", inbound=False)
        self.threads.setdefault(subject_id, []).append(msg)
        return msg.id
