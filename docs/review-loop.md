# The review loop

How a human's correction becomes a prompt edit, and the two design mistakes that shaped it.

## The problem it solves

Before the loop existed, this is what happened when a draft was wrong. The reviewer opened a
separate chat session, pasted the draft, explained what was wrong, got a corrected version
back, pasted that into the real conversation by hand, and wrote a line in a log file.

Look at what was produced and then thrown away. In that separate session the reviewer
generated a complete training triple - **the wrong draft, an explanation of what is wrong,
and the correct text** - authored by the one person qualified to write it, at the exact
moment of judgement. All of it lived in a transcript that the tooling deleted after 30 days.

The feedback was not merely unused. **It expired.**

And the verdicts were lossy in a second way: three different outcomes collapsed into one
Decline button, so a month of declines was an inseparable mixture of "the draft was wrong, I
sent my own" and "no reply should go out at all" - opposite conclusions about what to change.

## The principle

> *The explanation typed into a separate session should be typed into the card instead.*

Same keystrokes, different destination, and the destination is what makes it an asset.

## Three buttons, three signals

**Approve** sends.

**Fix** takes one line of "what is wrong", runs a correction pass, and returns a new version
*in the card's thread*. Never an automatic send, and never overwriting the draft it corrects,
so the before and after both stay readable.

**Decline** takes one required line: why no reply?

The three signals differ in kind, not in severity, and the third is *derived* rather than
asked for:

| Signal | How it arises | What it means | Where the fix goes |
|---|---|---|---|
| Fix | the reviewer corrects the draft | routed right, written wrong | the generator prompt |
| Declined, no fix | declined with no correction attempt | the system was right to have nothing sendable | the entry gate |
| Declined after a fix | corrected, then declined anyway | **no correct automated reply exists** | the taxonomy, or the scope of the product |

The last one is the most valuable and the hardest to get. A Fix improves wording. A decline
that *survives* a correction says the model of the world is incomplete.

## Mistake one: asking for the signal instead of deriving it

The first version asked for that third signal directly - a radio button on the Decline
dialog offering "escalate: no good reply exists".

It returned **zero escalations in a week, against 108 plain declines.**

Nobody used it, because at the moment of declining you are trying to clear a queue, not to
classify your own reasoning. Deriving the same signal from what the reviewer actually *did* -
they pressed Fix, then declined anyway - is cheaper and is a stronger claim than a button
they would have skipped. The radio was removed.

## Mistake two: an optional field

The same week taught the other half. The Decline reason was optional.

**Not one of 108 reviewers filled it in.**

And the weekly pass - whose contract calls the reviewer's words the ground truth - had
nothing to read, so it invented a pattern out of the tag name alone and put a confident,
well-argued, entirely fabricated proposal up for human approval.

**Optional means empty.** The field is required now, and `runtime/review.py` refuses to build
a proposal from a cluster with no reviewer words rather than proceeding without them.

## The correction pass is a third pass, not a re-run

The generator applies templates. Correcting a draft against a critique is reasoning over
constraints, and a template-applier asked to do it will satisfy the critique by breaking a
different rule.

So the correction pass gets its own prompt, a more capable model, and one extra requirement:
**it must name the rule the draft violated before it emits any text.** That makes the named
rule the cluster tag - in the taxonomy the prompt already uses, confirmed for free on a click
the reviewer was already making.

It runs on roughly one card in eight, which is what makes a pricier tier affordable there.

## Bind the tag to the document, not to the label

This is the subtlest bug in the whole loop and it is worth the detail.

The obvious contract for the correction pass is "return the name of the rule you violated".
That contract is **unverifiable**, and in production it was wrong 12 times out of 42:

- The policy document carried two independently numbered lists, both starting at 1. "Rule
  16" named a different rule in each, and nothing could tell them apart.
- Some numbers were out of range entirely - a rule that did not exist. A string is a string.
- An entire cluster of pricing corrections was filed under a rule about fabricating link
  content. The weekly pass then proposed editing *that* rule. The proposal was coherent,
  well argued, and aimed at the wrong paragraph.

None of that is visible downstream. **The batch looks identical whether the binding held or
not**, which is what makes it dangerous rather than merely wrong.

The fix: the correction pass returns the rule's **opening line, copied character for
character** from the policy it was given. The document then supplies the label - we look the
quote up in the live prompt file and read the rule's identity off the file itself. A
paraphrase, a fixed typo or a renumbered prefix fails the lookup, and a failed lookup is
*reported* rather than silently accepted.

The model's job shrinks from "know which rule this is" to "copy this line", which is the job
models are reliable at. And the bound/unbound split becomes a number in the weekly header:
the difference between *fixed* and *believed fixed*.

An **ambiguous** quote - one that matches two rules - is a failure, not a coin flip. Two
rules sharing an opening clause are exactly the case where guessing puts a finding on the
wrong paragraph. See `tests/test_learning_loop.py`.

## The cadence has a reason

One correction is an anecdote. Two of the same tag is a pattern.

At roughly seventy interventions a week, a *daily* pass sees ten, so almost every tag would
be a singleton. Acting on singletons is precisely how a prompt reaches seven hundred lines
of accreted special cases, each one a reasonable response to one bad Tuesday. **Weekly is the
filter.** A same-day trigger exists only for a tag that spikes, so a live regression is not
left running for six days.

## Applying an edit

The proposal is stored as an exact **anchor to replacement pair**, never as a diff. A diff
needs context matching and can apply in the wrong place or ambiguously. An exact string
either matches once or refuses.

That refusal is also the staleness guard: if the prompt changed between the proposal on
Thursday and the Apply press on Friday, the anchor is gone and the edit declines to land. A
refused edit is a safe outcome - the tag simply returns next week.

The same interface, one level up: its own channel, and three buttons mirroring the reply
card. Apply edits the prompt and commits it. Adjust takes one line and regenerates the
proposal on the same card. Reject requires a reason, or the tag returns next week anyway.

## Measure the loop, or it dies

A weekly ritual that asks for approvals without showing that last week's approvals changed
anything is a meeting. The header reports:

- **first-pass acceptance as a cohort on one denominator** - of the cards decided in this
  window, how many were approved with no edit at all. Deliberately not an event count over
  interventions: a numerator with no denominator cannot improve, and reporting one is how a
  metric ends up wrong by an order of magnitude while looking like it is being watched.
- **drafts nobody wanted**, as a share. An entry-gate number, reported here precisely because
  no prompt edit can move it. In production it was 18%, which made it the single largest
  source of wasted human attention - and a ritual that only ever proposed wording changes
  would never have surfaced it.
- **bound versus unbound findings**, the honesty check on the clustering itself.
- **per applied edit, recurrence before versus after**, normalised by volume. The raw counts
  are not comparable: that queue went from 37 to 565 items a week, so an unchanged failure
  *rate* shows up as many more occurrences and reads as "worse", while a genuine improvement
  can read as "back in the queue". The rate decides the verdict; both numbers are shown so a
  reader can see which one moved.

## What it produced

In the production system, over the first three weeks:

| | |
|---|---|
| Labelled interventions captured | 399 |
| Carrying the reviewer's own words | 289 |
| Candidates reaching the weekly gate | 20 |
| Prompt edits applied | 6 |

It also caught its own regressions. On one day it surfaced a rule that mandated a credentials
pitch on a deferral (11 of 13 corrections in a 53-minute stretch opened with the same
mandated sentence), then caught the correction for that going too far, then caught a third
rule refusing to do something a reviewer had explicitly asked for twice. Each within hours of
the change that caused it.

That is the argument for the loop, and it is not that it writes better prompts than a human.
It is that it notices, in hours rather than weeks, when a human wrote a worse one.
