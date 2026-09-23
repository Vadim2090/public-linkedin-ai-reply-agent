"""Time as a trigger.

A webhook-driven service can only ever react. It cannot notice that a conversation went
quiet, which is the single most common thing that happens to a conversation. This module is
the sibling engine that can: it scans on a timer, decides who is worth nudging, and produces
the same approval card the reactive engine produces.

IT SHARES THE RAILS AND KEEPS ITS OWN POLICY. Same store, same approval primitive, same
review surface, same approve-then-send mechanic. What is its own: the entry criteria, the
suppression rules, the cadence, its own send lock and its own approver list. That division
is what made a third engine cost one policy registration instead of four new branches.

BUILT FROM A MEASUREMENT, NOT AN INTUITION. In the production system the case was: of 1,975
conversations, 1,258 ended with OUR message; 433 of those had a link in the thread and three
or more days of silence; 312 carried a positive-reply marker. Twelve weeks had produced
thirty manual nudges, none of them automated. The number that justified building it was the
gap between 312 and 30, and it was worth an afternoon of SQL to find before writing any code.

EVERY SKIP CARRIES A REASON. This is the part worth copying. The production engine recorded
126,406 skips, each with the rule that caused it. That is not logging for its own sake:
"we skipped silently" and "nothing was eligible" are the same observation from the outside,
and the difference between them is a whole cohort quietly dropped. A skip table with reasons
is how you notice that a suppression rule you added last month is eating everyone.
"""
import logging
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import store, transport

log = logging.getLogger("runtime.scheduler")


@dataclass(frozen=True)
class Rule:
    """One reason not to nudge someone. Name it, because the name is what gets counted."""
    name: str
    #: (subject_id, thread, now) -> True if this rule BLOCKS the nudge.
    blocks: Callable[[str, List[transport.Message], float], bool]
    #: Shown to the reviewer and stored in the skip ledger.
    explain: str = ""


def quiet_days(thread: List[transport.Message], now: float) -> float:
    """Days since the most recent message, from the thread itself rather than from a
    field someone has to remember to update."""
    msgs = transport.ordered(thread)
    if not msgs:
        return 0.0
    last = msgs[-1].sent_at or ""
    try:
        parsed = time.mktime(time.strptime(last[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0.0
    return max(0.0, (now - parsed) / 86400.0)


# ------------------------------------------------------------------ the standard rules

def standard_rules(min_quiet_days=3.0, max_touches=3, dnc=frozenset()) -> List[Rule]:
    """The suppression set every scheduled engine needs, whatever it is nudging.

    ORDER IS PART OF THE CONTRACT, in two ways, and both were learned by getting it wrong.

    Cheapest first, because only the first matching rule runs: a do-not-contact check is a
    set lookup, while counting previous touches is a query, and there is no point paying for
    the second when the first already said no.

    More importantly, MOST SPECIFIC FIRST. The first version of this list put `too_recent`
    above `empty_thread`, and an empty conversation has a quiet-time of zero, so every
    subject with no history was filed under "nudged too recently". The engine's behaviour
    was correct and its explanation was a lie - which is the exact failure the skip ledger
    exists to catch, so it is fitting that a test caught it. When two rules would both
    block, the one that names the real reason has to come first.
    """
    return [
        Rule("do_not_contact",
             lambda s, t, n: s in dnc,
             "on the do-not-contact list, which is hand-curated and changes between runs"),
        Rule("empty_thread",
             lambda s, t, n: not transport.ordered(t),
             "nothing to follow up on"),
        Rule("they_spoke_last",
             lambda s, t, n: transport.should_reply(t),
             "they have replied; this is a conversation now, not a silence, and the "
             "reactive engine owns it"),
        Rule("too_recent",
             lambda s, t, n: quiet_days(t, n) < min_quiet_days,
             f"quiet for less than {min_quiet_days} days"),
        Rule("already_queued",
             lambda s, t, n: _queued(s),
             "a card for this subject is already waiting for a human"),
        Rule("touch_cap",
             lambda s, t, n: _touches(s) >= max_touches,
             f"already nudged {max_touches} times; a fourth is not persistence"),
    ]


def _touches(subject_id):
    with store._conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM approvals WHERE subject_id = ? AND kind = 'nudge' "
            "AND status = 'approved'", (subject_id,)).fetchone()[0]


def _queued(subject_id):
    with store._conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM approvals WHERE subject_id = ? AND kind = 'nudge' "
            "AND status = 'pending'", (subject_id,)).fetchone()[0] > 0


# ------------------------------------------------------------------------- the scan

def init_skips():
    with store._conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS skips (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id TEXT NOT NULL,
            rule       TEXT NOT NULL,
            detail     TEXT,
            at         REAL NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_skips_at ON skips(at)")


def record_skip(subject_id, rule: Rule):
    with store._conn() as c:
        c.execute("INSERT INTO skips (subject_id, rule, detail, at) VALUES (?, ?, ?, ?)",
                  (subject_id, rule.name, rule.explain, time.time()))


def skip_report(since_ts):
    """Which rules are doing the suppressing. Read this before adding another one.

    A rule that suppresses almost everything is either the only rule that matters or a bug,
    and the report is the difference between finding that out this week and finding it out
    when someone asks why the engine has gone quiet.
    """
    with store._conn() as c:
        rows = c.execute("SELECT rule, COUNT(*) n FROM skips WHERE at >= ? "
                         "GROUP BY rule ORDER BY n DESC", (since_ts,)).fetchall()
    return [(r["rule"], r["n"]) for r in rows]


def scan(subject_ids, tr: transport.Transport, rules: List[Rule],
         generate: Callable[[str, List[transport.Message], int], Optional[dict]],
         now=None):
    """One pass. Returns the approvals created.

    `generate(subject_id, thread, touch_number) -> draft dict or None` is injected: what a
    nudge SAYS is domain knowledge and does not belong in the runtime.

    Note the shape of the loop. Every subject either produces a card or produces a skip row.
    There is no third path, and that is deliberate: a subject that falls out of a scan
    silently is indistinguishable from a subject that was never scanned.
    """
    now = now or time.time()
    init_skips()
    created = []
    for subject_id in subject_ids:
        thread = tr.fetch_thread(subject_id)
        blocked = next((r for r in rules if r.blocks(subject_id, thread, now)), None)
        if blocked:
            record_skip(subject_id, blocked)
            continue
        touch = _touches(subject_id) + 1
        draft = generate(subject_id, thread, touch)
        if draft is None:
            record_skip(subject_id, Rule("no_draft", lambda *a: True,
                                         "the generator declined to write one"))
            continue
        draft.setdefault("subject_id", subject_id)
        draft["touch"] = touch
        created.append(store.save_approval(draft, kind="nudge", subject_id=subject_id))
    return created


# ------------------------------------------------------------------- the click-time gate

def pre_send_recheck(approval_row, tr: transport.Transport, dnc=frozenset()):
    """The check that the entry scan cannot do, because it runs at a different time.

    Everything on a nudge card was decided when the card was POSTED, and a card can sit for
    days waiting for a human. In that window the other party can reply, the situation can
    resolve itself, and someone can be added to the do-not-contact list. Approving then
    sends text written against a world that no longer exists.

    Deliberately NARROW. It does not re-run the whole entry gate: most of that decides
    whether someone is WORTH nudging, which does not change in three days. It re-asks only
    the questions whose answer makes sending actively WRONG.

    FAILS CLOSED. If a check cannot be made, refuse. An unreachable system must never read
    as "nothing has changed". The cost of a refusal is a delay; the next scan reconsiders
    them. The cost of the alternative is a message to someone who already answered.
    """
    subject_id = (approval_row or {}).get("subject_id")
    if not subject_id:
        return "the card carries no subject, so nothing can be re-checked"
    try:
        thread = tr.fetch_thread(subject_id)
    except Exception as e:
        return f"the conversation could not be re-read ({e}); refusing rather than guessing"
    if transport.should_reply(thread):
        return "they have replied since this card was written"
    if subject_id in dnc:
        return "now on the do-not-contact list"
    return ""
