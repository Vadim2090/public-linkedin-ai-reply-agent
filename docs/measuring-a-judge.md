# Measuring a judge, and deciding not to ship it

A pre-registered offline study that tried to make the human gate cheaper, and failed. The
negative result is the reason this document exists: it is the part of the work that decided
what *not* to build.

## The question

Reviewing approval cards cost the reviewer 30 to 90 minutes a day, on 60 to 80 cards.

The proposal was obvious and appealing: add a pass that scores each draft's readiness to
send, so the reviewer reads a verdict instead of a conversation. If it worked, the same
mechanism could eventually replace the human gate for the easy cases.

Stated as a testable claim:

> Is there a threshold at which a model-scored verdict of READY is correct at least 95% of
> the time, over a share of the queue large enough to be worth wiring?

The decision rule was fixed **before** any scoring ran, and so were the thresholds:

| Coverage at 95% precision | Decision |
|---|---|
| 40% or more | build the fast lane |
| 20% to 40% | build it for the safest intents only |
| under 20% | do not build it |

Writing that down first is the whole methodology. A judge produces a curve, and a curve read
after the fact always contains a point that looks like a result.

## Design

**Population.** 1,225 real cards, every decided card in a 21-day window, after excluding
follow-up and recovery engines and 70 cards whose conversation could not be faithfully
reconstructed.

**Ground truth is what the human actually did**, which is a behavioural label, not a
statement about factual safety:

| Label | Count | Share |
|---|---|---|
| clean - approved with no edit | 873 | 71.3% |
| fixed - corrected, then sent | 121 | 9.9% |
| declined - no reply wanted | 231 | 18.9% |

A READY verdict on a card that was fixed or declined is a **false accept**. That is the
number that matters: it is a message that would have gone to a real person unread.

**Leakage control.** Each conversation was rebuilt from the transport's history and **cut at
the moment the card was created** - keeping only messages at or before that timestamp. Our
sent reply and any later inbound message are exactly the evidence that would tell the judge
the answer, so leaving them in would have produced a beautiful and meaningless result. Only
the original draft was scored, never the human-corrected version.

**The judge**, three parts, one call per card:

1. a blind re-classification of the intent, with no sight of the pipeline's own label
2. one yes/no question per policy rule the draft might have broken, plus one asking whether
   a reply was warranted at all
3. a three-level readiness score

Reasons come from question keys, not from generated prose, so a verdict is checkable by a
human in seconds.

**Three routes**, in increasing strictness: readiness alone; readiness plus every rule
question clear; and both plus agreement with the pipeline's intent and no deterministic
pre-check hits.

A deterministic linter ran before the model on every card: banned words, more than one link,
typography, a name used when no name was known.

## The zero-cost baseline first

Before any model call: the pipeline already stores an intent and a confidence on every card.
If some combination of those were reliably clean, a fast lane would cost nothing at all.

**No intent-by-confidence cell with n >= 30 reached 95% clean approval.** The nearest was a
confirmation-shaped intent at 100% clean on 11 cards - too few to act on.

That is a real finding and it deserves stating plainly: the confidence score this system has
computed on every card for a year cannot gate anything by itself.

## Results

Base rate: 71.3% of cards were clean. Any judge has to beat that by enough to change what
the reviewer does.

**General-purpose model** (`gpt-5-mini`, one call per card, $0.95 per 1,000 cards):

| Route | Threshold | READY | Coverage | Precision | 95% CI |
|---|---|---|---|---|---|
| readiness | 0.60 | 219 | 17.9% | 85.4% | 80.1 - 89.5 |
| readiness | 0.75 | 111 | 9.1% | 91.9% | 85.3 - 95.7 |
| readiness | 0.85 | 86 | 7.0% | 94.2% | 87.1 - 97.5 |
| readiness | 0.90 | 63 | 5.1% | 92.1% | 82.7 - 96.6 |
| rule battery | 0.75 | 30 | **2.5%** | **96.7%** | 83.3 - 99.4 |
| rule battery | 0.85 | 24 | 2.0% | 95.8% | 79.8 - 99.3 |

The only basket clearing 95% covers 2.5% of the queue - 30 cards, 29 of them clean - and its
confidence interval reaches down to 83%. That is not evidence of 95% precision. It is one
good week.

**A purpose-built structured-decision model** (TypeSafe's `jev-1.13`, early access, $0.26
per 1,000 cards, 0.5 s per call) was run on the identical cards and battery:

| Route | Threshold | READY | Coverage | Precision |
|---|---|---|---|---|
| readiness | 0.60 | 860 | 70.2% | 75.8% |
| readiness | 0.75 | 297 | 24.2% | 80.1% |
| readiness | 0.85 | 32 | 2.6% | 78.1% |
| rule battery | 0.75 | 13 | 1.1% | 53.8% |

At a low threshold it calls 70% of the queue READY at 75.8% precision - which is the base
rate, so the verdict carries almost no information. At a high threshold it admits almost
nothing. Its only basket above 95% held two cards.

One thing it did better: its blind intent agreed with the pipeline's classifier on **72.3%**
of cards against the general model's **57.4%**. It reads a fixed taxonomy more like the
system's own classifier does. That is a genuine strength for a classification job, and it is
not the job being tested here.

**Fairness caveat, stated because the comparison is public.** This is one task, one battery
written for a general model, replayed offline. It is not a general verdict on either model,
and the structured-decision model was being asked to do the thing its own documentation is
least enthusiastic about. Its cost and latency advantages are real and large. They were
simply irrelevant here: at 430 cards a week the entire spend is about a dollar.

## The decision

**Do not build the fast lane.** Coverage at 95% precision is 2.5%, far below the 20% floor
fixed in advance.

Total cost of finding that out: **$1.49 and a day.** The feature it prevented would have
been weeks of work, and would have shipped a verdict the reviewer learned to trust.

## What the failure taught, which is the useful part

**The errors are not where the idea assumed.** At low thresholds the false accepts are
dominated by cards the human *declined* - drafts nobody wanted. That decision depends on
state outside the conversation: the person already booked a call, a colleague answered by
hand, the deal moved. **No judge reading only the thread can reach it**, and no amount of
model capability changes that. The 18% of cards that are unwanted drafts are an entry-gate
problem, and the study is what turned that from a suspicion into the next piece of work.

**Disagreement is a better signal than confidence.** Where the blind re-classification
disagreed with the pipeline's own label, false accepts roughly doubled. That is a usable
"a human should read this one" flag even though the READY verdict is not usable. Two
independent readings disagreeing is information; one reading's self-reported confidence is
much weaker.

**A judge that agrees with you is not measuring you.** The two judges' READY sets barely
overlapped - one card in common at the strictest useful threshold. Had only one been run, its
curve would have looked like a property of the task. It was a property of the judge.

**Automation bias was the risk that did not get to happen.** A verdict on the card changes
how the reviewer reads the card. The plan included measuring override rates by verdict bucket
from day one, precisely because a fast lane that silently stops being checked is worse than
no fast lane. That instrumentation was never needed, but designing the rollout around it is
why the negative result was cheap.

## Reproducing the shape

This repository does not ship the study - it ran against private conversations. The shape is
reproducible against any approval corpus:

1. Snapshot the approvals table. Label each decided card clean / fixed / declined.
2. Rebuild each conversation and **cut it at the card's creation time**.
3. Fix your thresholds and your decision rule, in writing, before scoring anything.
4. Score the original draft only.
5. Report coverage and precision with confidence intervals at every threshold, and the
   baseline you already had for free.
6. Compare the judge's independent classification against your own classifier's.

Step 3 is the one people skip, and it is the one that makes the answer mean anything.
