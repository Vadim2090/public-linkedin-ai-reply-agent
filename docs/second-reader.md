# An independent second reading of classification

This experiment asks whether disagreement from a purpose-built structured-decision model
can direct a reviewer toward classifier mistakes. It uses synthetic conversations only. A
positive result would validate the mechanism on this set, not establish production impact.

## What it does

With `TYPESAFE_API_KEY` set, `example/pipeline.py` asks TypeSafe's Jev (`jev-1.13.0`,
pinned) to classify the newest inbound message after `classify-v1` has run. The reader sees
the thread with timestamps removed and never sees the pipeline's answer. The criteria live
in `example/prompts/second-reader-v1.json`, one entry per intent plus `unclear`, with the
priority rules written out as explicit `not_when` conditions.

The result is advisory. It never changes the route, the arm, the draft or whether a card is
posted. A card gets one extra line only when the reading is flagged:

```
:eyes: Second reader: <choice> (p=0.xx); pipeline said <intent>
```

Nothing is shown when the readings agree, so there is no "safe" badge for a reviewer to lean
on. A timeout, a rate limit after retries, or an authentication or validation error shows
`Second reader unavailable` and the card goes out as before. A terminal intent produces no
card, so a flagged reading is recorded in the `no_draft` reason instead.

| Setting | Effect |
|---|---|
| `TYPESAFE_API_KEY` unset | Off. The pipeline output and the demo are unchanged, and no socket is opened |
| `SECOND_READER=off` | Off, even with a key |
| `TYPESAFE_REPLAY=<file>` | Answers from recorded responses keyed by a hash of the request body, with no network |

The client is `runtime/typesafe.py`: stdlib `urllib`, retries on 408, 429, 5xx and 529, no
retry on other 4xx, and error messages that never carry the key.

## Pre-registration

The pipeline classifier is `classify-v1` through a real provider (`PASS1_PROVIDER=openai`
or `azure`); the harness refuses a scored run on the keyword stub. Reasoning models may need
more than the runtime's 500-token completion default to return any JSON, so the harness sets
`PASS1_MAX_TOKENS=1200` unless it is already set. A case is flagged when the answers
disagree, Jev chooses `unclear`, or Jev confidence is below `0.60`.

The 264 authored conversations contain 24 cases per intent and 12 hard cases for each of
priority rules 1 through 6. Rule 7 concerns the confidence field, which a gold intent label
cannot test. A fixed seed (`113`) makes a stratified split of 7 dev and 17 test cases per
intent: 77 dev and 187 test. Criteria may be tuned on dev only. Test is run once, after the
dataset and prompt hashes below are frozen in git.

| Frozen artifact | SHA-256 |
|---|---|
| `example/eval/cases.jsonl` | Pending the 40-label review |
| `example/prompts/second-reader-v1.json` | Pending the 40-label review |

The fixed review sample is in [second-reader-label-review.md](second-reader-label-review.md).
More than two wrong labels triggers a full review before the hashes are recorded.

## Decision rule

On the test split, let E be pipeline errors and F be flagged cases. The thresholds are
constants at the top of `tools/measure_second_reader.py`, committed before any scored run.

| Outcome | Rule |
|---|---|
| Inconclusive, checked first | Fewer than 15 pipeline errors. Author one new hard batch blind to outputs and rerun once; if still under 15, stop. |
| Success | Error recall at least 50%, flag rate at most 30%, and lift at least 2.0. |
| Kill | Lift below 1.5, error recall below 30%, or flag rate above 40%. |
| Marginal | Anything between the success and kill rules. |

Error recall is `|E and F| / |E|`. Flag rate is `|F| / n`. Lift is
`P(error given F) / P(error given not F)`; when every error is flagged it is unbounded and
clears any lift bar. Wilson 95% intervals, confusion tables, both readers' accuracy, flags by
cause, Jev input tokens, the cost per 1,000 messages at $0.042 per million input tokens, and
the classifier's provider usage are reported alongside the decision.

The 2.0 lift bar is a judgment call, not a replication target. It echoes the "roughly
doubled" in [measuring-a-judge.md](measuring-a-judge.md), but that figure measured bad
drafts on production data, not classifier errors.

## How to run

```
python3 tools/measure_second_reader.py --split dev --summary     # as often as needed
python3 tools/measure_second_reader.py --split test --summary    # once; writes test-results.json
```

`--record-replay` stores every Jev response in `example/eval/replay.json`, so a run can be
repeated offline with `TYPESAFE_REPLAY` set. The test split refuses to run a second time
once `example/eval/test-results.json` exists.

## Results

Not run. The label review and the frozen hashes come first.

## Privacy and limits

The fixtures are synthetic and use the public example domain. No production messages,
logs, decisions, or approvals are used. Enabling the live option in a fork sends message
content to another processor; review TypeSafe's data terms for the applicable plan first.
