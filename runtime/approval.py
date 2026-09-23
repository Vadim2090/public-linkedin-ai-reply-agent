"""Human approval as a reusable primitive.

The insight this file exists to make concrete: once more than one engine wants a human to
sign off on something, "ask a human" stops being a feature of one pipeline and becomes a
runtime the pipelines are clients of.

In the system this was extracted from there were three engines by the end: a reactive one
answering inbound messages, a scheduled one nudging stalled conversations, and a third
reacting to CRM state. All three ended in the same card, on the same interactivity URL,
through the same approve-then-send path. What differed between them was small and entirely
declarative: which approver list, which send lock, what to write afterwards, whether
correcting the draft is even meaningful.

Before this file, those differences lived as `if kind == "followup"` at FOUR separate call
sites. That is tolerable with two engines and a bug with three: adding a kind means
remembering all four, and the one that is easy to forget is the send lock, which fails by
SENDING while the engine's own flag reads false.

So the branch moves here, once, as data. A new engine adds one `Policy` and touches no
control flow anywhere else.

TWO PROPERTIES WORTH KNOWING.

1. **Every field is a callable, not a value.** The send lock is read at CLICK time. A card
   can sit for days; snapshotting a boolean when the process started would freeze the lock
   at whatever it was then, and would make the flag untestable.

2. **An unknown or unloaded kind cannot send.** It used to fall back to the default reply
   policy, and that was a hole big enough to defeat the whole table: the default policy's
   lock is open in production, so a card belonging to an engine whose module failed to
   import would have SENT, consulting neither that engine's own off switch nor its ledger.
   An import error would have silently bypassed a safety flag. Now it falls back to a
   QUARANTINE policy: resolvable, never sendable.
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

log = logging.getLogger("runtime.approval")


@dataclass(frozen=True)
class Policy:
    """One approval kind's answers to the four questions a button click asks."""

    kind: str
    #: Named in "you are not on the approver list for {label}". Plural, lowercase.
    label: str
    #: The env var named in the send-locked footer, so a reviewer knows which flag to flip.
    send_lock_name: str
    #: Who may click. Called at click time.
    approvers: Callable[[], List[str]]
    #: May this kind send right now? Called at click time.
    send_allowed: Callable[[], bool]
    #: Whether correcting the draft is meaningful for this kind. See the note below.
    allow_fix: bool = True
    #: Last check before the text leaves, run at CLICK time. Returns a refusal string, or
    #: "" to proceed. Everything else a card carries was decided when it was POSTED, which
    #: can be days earlier. This is the only hook that sees the world as it is at the
    #: moment of sending.
    pre_send: Optional[Callable[[dict], str]] = None
    #: Resolved without sending (declined, or approved under a send lock). Let the
    #: engine's queue go, so the subject is reconsidered on the next run.
    on_release: Optional[Callable[[str], None]] = None
    #: Text actually reached the recipient. Write the engine's ledger.
    on_send: Optional[Callable[[dict, str], None]] = None
    #: Appended to the sent footer, e.g. " (nudge 2 of 3)".
    sent_suffix: Callable[[dict], str] = field(default=lambda appr: "")


class Registry:
    """The table of policies. A new engine is one `register` call."""

    def __init__(self, default: Policy):
        self._default = default
        self._policies = {default.kind: default}

    def register(self, policy: Policy):
        self._policies[policy.kind] = policy
        return policy

    def for_kind(self, kind) -> Policy:
        """Three cases, and only the first is normal:

        * kind is None or the default kind -> the default policy
        * kind is a registered engine       -> that engine's policy
        * anything else                     -> QUARANTINE, which cannot send

        The last case covers both a known engine whose module failed to import and a kind
        nobody recognises, because from here the two are indistinguishable and both must
        fail closed.
        """
        k = kind or self._default.kind
        p = self._policies.get(k)
        if p is not None:
            return p
        log.error(
            "no policy registered for approval kind %r - this card CANNOT SEND. Its engine "
            "failed to import or its kind is unrecognised; approving will close the card "
            "and send nothing. Fix the import and restart.", k)
        return self._quarantine(k)

    def for_approval(self, approval: dict) -> Policy:
        return self.for_kind((approval or {}).get("kind"))

    def loaded_kinds(self):
        """For a health endpoint: which engines actually registered in this process. An
        engine that silently failed to import is otherwise invisible until a card from it
        refuses to send."""
        return sorted(self._policies)

    def _quarantine(self, kind) -> Policy:
        """Resolvable, never sendable.

        Somebody has to be able to close the card, and a button click must not return a
        500 - so Decline still works and the reviewer gets a truthful message. Only
        sending is refused.
        """
        return Policy(
            kind=kind,
            label=f"{kind} (engine not loaded)",
            send_lock_name=f"{str(kind).upper()}_SEND_ENABLED",
            approvers=self._default.approvers,
            send_allowed=lambda: False,
            allow_fix=False,
        )


def may_click(policy: Policy, user_id: str) -> bool:
    """Authorisation, kept separate from authentication on purpose.

    A valid signature on the request proves the message came from the chat platform. It
    says nothing about whether THIS person may approve. Two independent layers: the
    transport verifies the request, this verifies the human.

    The approver list DEFAULTS TO EMPTY. An unconfigured deployment approves nothing,
    rather than approving for everyone - the failure mode of a permissive default here is
    a stranger in a shared channel sending messages under your name.
    """
    return user_id in (policy.approvers() or [])
