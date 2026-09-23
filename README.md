# approval-gated-agent

A small, runnable implementation of a pattern that turns out to be the interesting part of
most LLM products people actually ship: **a model drafts, a human approves, and the approval
is kept as training data.**

Not an agent framework. About 1,500 lines, five standard-library imports, no dependencies at
all for the offline path.

```bash
git clone <this repo> && cd approval-gated-agent
python3 -m example.demo          # no API key needed
python3 -m unittest discover -s tests -t .
```

The demo drafts replies to three inbound messages, prints the approval cards to your
terminal, then drives one of each verdict and shows you the weekly learning report. Every
model call is a deterministic local function unless you point it at a provider.

## Where this comes from

This is the domain-free layer of a system that has been in production since July 2026,
handling inbound replies for a US immigration-services company. That system is private: its
prompts carry a named individual's professional identity, its logs carry real customers'
names, and its policy file is the company's commercial playbook. None of that can be made
public by deleting files, because the git history keeps it.

So this repository is a **fresh extraction, not a redaction**. The transport, the
orchestration, the approval primitive, the concurrency guards and the learning loop are the
same design. The domain is an invented support inbox.

Numbers from the production system, read from its state database on 2026-09-22:

| | |
|---|---|
| Approval cards issued | 3,268 |
| Decided reply cards | 2,329 |
| Approved with no edit | 76.7% |
| Corrected by a reviewer, then sent | 5.3% |
| Declined, no reply wanted | 18.0% |
| Peak week | 565 cards |
| Human corrections captured as labelled examples | 399 |
| Prompt edits applied through the weekly gate | 6 |

## The idea worth stealing

> **A human approval gate is a free labelled-data generator, but only if the label is
> persisted next to the input that produced it.**

Most review workflows throw the label away. A reviewer rejects a draft, rewrites it
somewhere else, sends their version, and the single most valuable artifact in the whole
system - the wrong draft, an explanation of what is wrong, and the correct text, produced by
an expert at the moment of judgement - evaporates into a chat log that expires in 30 days.

The fix is not more tooling. It is putting the correction box **on the card**, so the
explanation the reviewer was going to type anyway lands somewhere durable. Same keystrokes,
different destination, and the destination is what makes it an asset. Any design that adds a
second place to visit decays.

Four rules follow, and they are the spine of this repository:

1. **Persist input and label in one record, at decision time.** A label written later is a
   label written from memory.
2. **Make capture cheaper than the reviewer's memory.** If the required field is optional,
   it will be empty. In production it was optional, and 108 reviewers out of 108 left it
   blank, and the weekly pass obligingly invented a pattern out of nothing.
3. **Treat the SECOND occurrence as the threshold for changing the system.** One correction
   is an anecdote. Acting on n=1 is how a prompt grows to 700 lines of accreted special
   cases, each one a reasonable response to one bad Tuesday.
4. **Deploy the fix without a release**, or the loop stalls at its last step. Prompts are
   read fresh from disk on every call here for exactly that reason.

## What is in here

```
runtime/          the part that does not care what business you are in
  store.py        three concurrency primitives, one SQL statement each
  approval.py     human approval as a registerable primitive, not an if-statement
  policy.py       bind a finding to a LINE of the prompt, not to a label a model typed
  review.py       cluster a week of corrections into at most a few proposed edits
  surface.py      chat as the entire UI: signing, the 3-second rule, card fallback
  llm.py          raw HTTP, and provider/model/mode treated as one decision
  transport.py    the two methods you implement to plug in your own channel

example/          an invented support inbox, to have something to be about
  pipeline.py     two passes, routing in code, one deterministic chokepoint
  prompts/        classify, generate, correct, propose-an-edit
  demo.py         the whole loop, offline

tests/            63 tests, no framework, stdlib unittest
```

## The five decisions that matter

**Two passes, not one.** Classification is separated from generation because in the
single-pass version almost every failure came from misreading the state of the conversation,
and that error then cascaded into a confidently wrong reply. Separating them lets a cheap
model classify, lets a terminal state skip the expensive call entirely, and puts the
classifier's own reasoning on the card - which is what lets a reviewer tell a routing bug
from a wording bug. Those get fixed in different places.

**A prompt rule broken twice belongs in code.** The failure is the rule's *location*, not
its wording. Rewording buys a lower failure rate; it never buys zero. So the boundary between
prompt and code is not fixed at design time, **it moves** as failures accumulate, and that
migration is normal operations rather than an admission of bad prompting. Four rules made
that journey in production: typography normalisation, which experiment arm a subject is in,
whether to reply at all, and the intent-to-route table.

**Fail closed, everywhere it is cheap to.** An unconfigured signing secret refuses every
request instead of accepting every request. An empty approver list authorises nobody. An
engine whose module failed to import gets a policy that can close a card but cannot send
one - because the version that fell back to the default policy meant an import error would
silently bypass a safety flag that read `false`.

**Three concurrency primitives, one SQL statement each.** A trailing debounce is
`INSERT OR REPLACE` on a primary key. A distributed lock is `INSERT OR IGNORE` plus a
rowcount check. Optimistic concurrency on approval is a conditional `UPDATE`. Each replaces
infrastructure people normally reach for, and each is one statement because that is what
makes it correct: the atomicity belongs to the database, not to us.

**Measure the loop, or it dies.** A weekly ritual that asks for approvals without showing
that last week's approvals changed anything is a meeting. The header reports acceptance as a
cohort over one denominator, how often a finding actually bound to a line of the policy
(the difference between "fixed" and "believed fixed"), and, per applied edit, recurrence
before versus after, normalised by volume.

## What this does NOT do

Honest limits, because they are the first questions a reviewer asks.

- **It does not decide whether a reply is wanted at all.** In production, 18% of decided
  cards were drafts nobody wanted, and that is the single largest source of wasted human
  attention. The decision depends on state outside the conversation, so no judge reading
  only the thread can reach it. [docs/measuring-a-judge.md](docs/measuring-a-judge.md) is
  the study that establishes this, with numbers.
- **It has no cost instrumentation.** Tokens are counted nowhere.
- **It is not a framework.** There is no plugin system and no configuration language. Fork
  it, delete the example, write your own pipeline against `runtime/`.
- **The confidence score routes nothing.** It is computed and displayed. On 1,295 real
  cards, no intent-by-confidence cell with n >= 30 reached 95% clean approval, so it cannot
  gate anything on its own.

## Further reading

- [docs/design.md](docs/design.md) - the architecture, and why each piece is shaped the way
  it is
- [docs/review-loop.md](docs/review-loop.md) - the learning loop in detail, including two
  design mistakes and what they cost
- [docs/measuring-a-judge.md](docs/measuring-a-judge.md) - a pre-registered study that tried
  to automate the human gate away, and failed. The negative result is the point.

## Licence

MIT. See [LICENSE](LICENSE).
