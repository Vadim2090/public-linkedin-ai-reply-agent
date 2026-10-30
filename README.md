# public-linkedin-ai-reply-agent

[![test](https://github.com/Vadim2090/public-linkedin-ai-reply-agent/actions/workflows/test.yml/badge.svg)](https://github.com/Vadim2090/public-linkedin-ai-reply-agent/actions/workflows/test.yml)

CI runs the tests on 5 Python versions (3.10 to 3.14) and a gitleaks secret scan of the full
git history on every push and pull request. The suite is fully offline: no network, no API
keys, no LLM calls.

The public extraction of a LinkedIn reply agent I have been building and operating since
**June 2025**: someone replies to a cold outreach message, a model drafts an answer, a human
approves it, and **the approval is kept as training data.**

That third clause is the one most review workflows drop, and it is the reason this
repository exists.

Not an agent framework. About 2,600 lines, standard library only, no dependencies at all for
the offline path.

```bash
git clone <this repo> && cd public-linkedin-ai-reply-agent
python3 -m example.demo          # no API key needed
python3 -m unittest discover -s tests -t .
```

The demo drafts replies to three inbound messages, prints the approval cards to your
terminal, then drives one of each verdict and shows you the weekly learning report. Every
model call is a deterministic local function unless you point it at a provider.

## Where this comes from

| | |
|---|---|
| **June 2025** | First version live: a single-pass prompt on a hosted automation platform (Zapier). No version control, prompts pasted into a web form. |
| **February 2026** | Single pass split into two: classify, then generate. Almost every failure had been a misread of the conversation cascading into a confident wrong reply. |
| **March 2026** | The work moves into git. Everything before this exists only as prompt text carried forward. |
| **July 2026** | Rewritten as a self-hosted service. The automation platform is bypassed on 31 July and retired on 7 August. |
| **August 2026** | A second engine ships: scheduled nudges, with time as a trigger. |
| **September 2026** | The learning loop closes: corrections become labelled data, and a weekly pass turns recurring ones into prompt edits. |

The production system is private and stays private. Its prompts carry a named individual's
professional identity, its logs carry real customers' names and addresses, and its policy
file is the company's commercial playbook. None of that can be made public by deleting
files, because the git history keeps it.

So this repository is a **fresh extraction, not a redaction.** The transport, the
orchestration, the approval primitive, the concurrency guards, the scheduled engine and the
learning loop are the same design, and so is the example domain: inbound replies to cold
outreach on a professional network. What is replaced is everything commercial - the company,
the product, the copy, the figures, the links and the objection scripts are placeholders.
Structure ports; content does not, and the content is the part that is not mine to publish.

## Operating numbers

Read from the production state database on 2026-09-22.

**Read the window before the counts.** That database was created on **2026-06-25**, when the
service was rewritten. The fourteen months before it, on the earlier automation platform, are
not counted here at all: that era had no state store, only a chat channel and a log file. So
these figures describe the current service's first three months, not the lifetime of the
system.

**Reply engine**, 2026-06-25 to 2026-09-21:

| | |
|---|---|
| Approval cards issued | 2,608 |
| Decided | 2,329 |
| Approved with no edit | 76.7% |
| Corrected by a reviewer, then sent | 5.3% |
| Declined, no reply wanted | 18.0% |
| Peak week | 565 cards |

**Scheduled nudge engine**, 2026-08-21 to 2026-09-22:

| | |
|---|---|
| Nudge cards issued | 659 |
| Approved and sent | 436 |
| Suppressed, each with a recorded reason | 126,406 |

**Learning loop**, since 2026-09-02:

| | |
|---|---|
| Human corrections captured as labelled examples | 399 |
| Carrying the reviewer's own words | 289 |
| Candidates reaching the weekly gate | 20 |
| Prompt edits applied | 6 |

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

## Two engines, one gate

The reactive engine answers inbound messages. It cannot notice that a conversation went
quiet, which is the most common thing that happens to a conversation, so a second engine
scans on a timer.

They share the store, the approval primitive, the review surface and the approve-then-send
mechanic. Each keeps its own entry criteria, suppression rules, cadence, send lock and
approver list. That division is what made a third engine cost **one policy registration
instead of four new branches** - see `runtime/approval.py`.

The scheduled engine was justified by a measurement rather than an intuition: of 1,975
conversations, 1,258 ended with our message, 433 of those had a link and three or more days
of silence, and 312 carried a positive-reply marker, against thirty manual nudges in twelve
weeks. The gap between 312 and 30 is what paid for the build, and finding it cost an
afternoon of SQL.

**Every suppressed subject leaves a reason.** 126,406 of them. That is not logging for its
own sake: "we skipped silently" and "nothing was eligible" are the same observation from
outside, and the difference between them is a cohort quietly dropped. The rule ordering in
`runtime/scheduler.py` carries a comment about the time this went wrong and filed every
history-less subject under the wrong reason - correct behaviour, lying explanation.

## What is in here

```
runtime/          the part that does not care what business you are in
  store.py        three concurrency primitives, one SQL statement each
  approval.py     human approval as a registerable primitive, not an if-statement
  scheduler.py    time as a trigger, and suppression that records why
  policy.py       bind a finding to a LINE of the prompt, not to a label a model typed
  review.py       cluster a week of corrections into at most a few proposed edits
  surface.py      chat as the entire UI: signing, the 3-second rule, card fallback
  llm.py          raw HTTP, and provider/model/mode treated as one decision
  transport.py    the two methods you implement to plug in your own channel

example/          inbound replies to cold outreach, with every value a placeholder
  pipeline.py     two passes, routing AND experiment arm in code, one chokepoint
  prompts/        classify, generate, correct, propose-an-edit
  demo.py         the whole loop, offline

tests/            81 tests, no framework, stdlib unittest
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
migration is normal operations rather than an admission of bad prompting.

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

**The experiment arm is resolved in code, before the generator is called.** A subject is
locked to one of two next steps, and which one is decided by a rule you can unit test: a link
already in the thread wins, otherwise a stable hash of the subject id. The generator used to
infer it from the wording, and it drifted - a message that merely SOUNDED like one arm flipped
a subject who had been sent the other. A real link beats prose, and reading it is three lines.

**Measure the loop, or it dies.** A weekly ritual that asks for approvals without showing
that last week's approvals changed anything is a meeting. The header reports acceptance as a
cohort over one denominator, how often a finding actually bound to a line of the policy
(the difference between "fixed" and "believed fixed"), and, per applied edit, recurrence
before versus after, normalised by volume.

## The study that failed, and why it is here

In September 2026 the obvious next move was to score each draft's readiness so the reviewer
could read a verdict instead of a conversation, and eventually skip the easy cards entirely.

It was tested properly: 1,225 real cards, thresholds and a decision rule fixed **in advance**,
conversations cut at the moment each card was created so the answer could not leak, and two
different judges - a general-purpose model and a purpose-built structured-decision model.

**The best basket clearing 95% precision covered 2.5% of the queue**, against a 20% floor
fixed beforehand. The feature was not built. Total cost of finding out: **$1.49 and one day.**

The failure is more useful than a success would have been: the errors sit in drafts nobody
wanted at all, and that decision depends on state outside the conversation, so no judge
reading only the thread can reach it. [docs/measuring-a-judge.md](docs/measuring-a-judge.md)
is the full write-up, including the numbers for both judges and the fairness caveats.

## What this does NOT do

Honest limits, because they are the first questions a reviewer asks.

- **It does not decide whether a reply is wanted at all.** 18% of decided cards were drafts
  nobody wanted, and that remains the single largest source of wasted human attention.
- **It has no cost instrumentation.** Tokens are counted nowhere.
- **It is not a framework.** No plugin system, no configuration language. Fork it, delete the
  example, write your own pipeline against `runtime/`.
- **The confidence score routes nothing.** On 1,295 real cards, no intent-by-confidence cell
  with n >= 30 reached 95% clean approval, so it cannot gate anything on its own.

## Further reading

- [docs/design.md](docs/design.md) - the architecture, and why each piece is shaped the way
  it is
- [docs/review-loop.md](docs/review-loop.md) - the learning loop in detail, including two
  design mistakes and what they cost
- [docs/measuring-a-judge.md](docs/measuring-a-judge.md) - the pre-registered study above

## Licence

MIT. See [LICENSE](LICENSE).
