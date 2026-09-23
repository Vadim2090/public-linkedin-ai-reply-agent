# Design

How the pieces fit, and why each is shaped the way it is. Every "why" below is a production
incident rather than a preference.

## The shape

```
inbound message
   -> trailing debounce           every new message resets the timer
   -> claim                       content-keyed; exactly one worker proceeds
   -> entry gate                  only if they have the last word
   -> PASS 1  classify            cheap model, JSON, intent + flags
   -> route resolution            a dict in code, not a paragraph in a prompt
   -> PASS 2  generate            the expensive model, one policy prompt
   -> normalise                   deterministic typography chokepoint
   -> approval card               chat message with three buttons
   -> Approve -> send
      Fix     -> PASS 3 correct -> new version in the card's thread
      Decline -> reason required
   -> weekly: cluster corrections -> PASS 4 propose one edit -> a human applies it
```

Four model calls, and only two of them are on the hot path. Pass 3 fires on roughly one card
in eight; Pass 4 fires once a week. That asymmetry is what makes it affordable to put the
most capable tier on the two that reason and the cheapest on the one that classifies.

## Why two passes

The single-pass version asked one model to work out the state of the conversation and write
the reply. Almost every failure came from the first half, and then cascaded: a confident,
well-written reply to a question nobody asked.

Splitting them buys four things.

- A cheap model does the classification. It is the higher-volume call.
- A terminal state short-circuits the expensive call entirely. Someone writing "thanks, got
  it" costs one cheap call and produces no card, so nobody is asked to approve a message
  nobody needs.
- The card can show the classifier's own reasoning, which is what lets a reviewer tell a
  **routing** bug from a **wording** bug. Those get fixed in different files, so conflating
  them wastes the correction.
- The two halves can be evaluated separately. "Did it understand?" and "did it write well?"
  have different answers and different remedies.

The cost is an extra call and an extra failure mode: the classifier can be confidently
wrong and the generator will faithfully write in the wrong register. That is what the human
gate is for, and what the Fix button turns into data.

## Routing belongs in code

`example/pipeline.py` has a dict:

```python
ROUTES = {
    "question":   ("answer_then_offer", 120),
    "bug_report": ("acknowledge_and_escalate", 90),
    ...
}
```

This was a paragraph in a prompt once. Three reasons it moved:

1. It is the thing reviewers most often mean to change, and a dict is diffable and testable.
2. A prompt asked to both choose a route and write in it will occasionally do neither well.
3. When it went wrong there was nothing to unit test. Now there is.

The general principle, and it is the one worth carrying to any LLM product:

> **A prompt rule broken twice belongs in code.** The failure is the rule's *location*, not
> its wording. Rewording buys a lower failure rate; it never buys zero. The boundary between
> prompt and code is therefore not fixed at design time - it **moves** as failures
> accumulate, and that migration is normal operations.

Four rules made that journey in the production system:

| Rule | As a prompt instruction | As code |
|---|---|---|
| No em-dashes | "NO em-dashes", in capitals, ignored for months | one normaliser every draft passes through, which keeps hyphens inside compound terms |
| Which experiment arm a subject is in | the generator inferred it from the thread | read deterministically; a real link beats wording |
| Whether to reply at all | implicit, and it sometimes answered itself | an explicit gate: only when the newest message is inbound |
| Intent to route | a table inside the prompt | a dict in Python |

The counter-case matters as much: anything that needs judgement about a specific person's
situation **stays in the prompt** and is caught by the human gate. That is what the gate is
for. Moving judgement into code is how you get a system that is confidently wrong in a new
way.

## The three concurrency primitives

Each is one SQL statement, and that is what makes it correct: the atomicity belongs to the
database. See `runtime/store.py` and `tests/test_concurrency.py`.

**A trailing debounce is `INSERT OR REPLACE` on a primary key.** Someone sends three
messages in ninety seconds. Answering the first is answering a question they have already
refined twice. Every arrival must push the deadline out and the work must run once, against
the final state. Because the table's primary key is the subject, the second arrival
overwrites the first row rather than adding a second, and the deadline moves with it. No
scheduler, no cancellation bookkeeping, no per-subject timer object to leak.

**A distributed lock is `INSERT OR IGNORE` plus a rowcount check.** The webhook fires twice,
or a retry lands while the first attempt is still running. Both workers would draft, both
would post a card, and a human would see the same subject twice. The database serialises the
two inserts; the loser inserted zero rows and learns that from `rowcount`. Without the
rowcount check the two calls are indistinguishable and the guard does nothing.

The key is derived from the newest message's **content**, not from the subject alone and not
from a random id. Keying on the subject would swallow their next message; keying on a random
id would defeat the guard.

**Optimistic concurrency on approval is a conditional `UPDATE`.**

```sql
UPDATE approvals SET status = 'approved'
 WHERE request_id = ? AND status = 'pending' AND version = ?
```

A double-click is not hypothetical: the review surface is a chat client, the button stays
live while the send is in flight, and a reviewer who sees no immediate feedback clicks again.
Read-then-write loses that race; this does not. The second click matches zero rows and the
caller sends nothing.

The `version` half closes a different hole. After a Fix produces v2, the v1 card is still
sitting in the channel with working buttons, and approving **there** would send superseded
text. The version travels in the button payload, so a stale click carries a stale version
and matches nothing.

## Chat is the entire interface

No web application was built. That is the single largest scope decision in the system: zero
UI code, zero auth to build, zero hosting, and the reviewer is already in the chat client
all day. A separate dashboard would be a second place to visit, and any review workflow with
a second place to visit decays.

Two things in `runtime/surface.py` are load-bearing.

**The three-second problem.** Chat platforms kill an interaction that is not acknowledged
within about three seconds, but sending a message takes longer. So: verify the request, hand
the work to a thread, return 200 immediately, and edit the card when the work finishes.
Doing the send inline shows the reviewer a timeout on an action that actually succeeded, and
then they click again.

**Three-tier card fallback.** A full card can be rejected for exceeding the platform's block
limits; a long conversation does it. When that first happened, no card appeared, nothing
logged an error, and a waiting customer was dropped on the floor. Now: full card, else a
minimal card with just the draft and the buttons, else a plain-text alert, and a total
failure is logged at ERROR. A degraded card is recoverable. A missing one is invisible.

Security is two independent layers, and they are often confused. A valid signature proves the
**request** came from the platform. It says nothing about whether **this person** may
approve. Both are checked, and both fail closed: no signing secret refuses every request,
and an empty approver list authorises nobody.

## Human approval as a primitive

Once a second engine wants a human to sign off on something, "ask a human" stops being a
feature of one pipeline and becomes a runtime the pipelines are clients of.

In production there were three engines: a reactive one answering inbound messages, a
scheduled one nudging stalled conversations, and a third reacting to CRM state. All three
ended in the same card, on the same URL, through the same approve-then-send path. What
differed was small and entirely declarative: which approver list, which send lock, what to
write afterwards, whether correcting the draft is even meaningful.

Before `runtime/approval.py`, those differences lived as `if kind == "followup"` at **four**
separate call sites. That is tolerable with two engines and a bug with three: adding a kind
means remembering all four, and the one that is easy to forget is the send lock, which fails
by **sending** while the engine's own flag reads `false`.

Two properties are worth copying.

**Every field is a callable, not a value.** The send lock is read at click time. A card can
sit for days; snapshotting a boolean at import would freeze the lock at whatever it was when
the process started.

**An unknown or unloaded kind cannot send.** This used to fall back to the default reply
policy, which was a hole big enough to defeat the whole table: the default policy's lock is
open in production, so a card belonging to an engine whose module failed to import would
have sent, consulting neither that engine's off switch nor its ledger. An import error would
have silently bypassed a safety flag. Now it falls back to a quarantine policy: closable,
never sendable, and it logs loudly.

## What is portable and what is not

| Ports | Does not port |
|---|---|
| The transport and orchestration layers | The intent taxonomy |
| The two-pass shape and its JSON contract | The policy prompt |
| Flags as a second routing axis | Pricing, compliance rules, objection scripts |
| An explicit precedence hierarchy | Anything a regulator would read |
| Deterministic chokepoints | |
| The review loop's shape | |

**Taxonomies are cheap; priority rules are expensive.** The intent list took an hour. The
classifier's priority rules - each one a conversation the system handled wrongly -
accumulated over months. On reuse the shape ports and the content does not, and anyone
promising otherwise has not run one of these in production.
