#!/usr/bin/env python3
"""Evaluate the independent classifier on the frozen synthetic set.

The test split is write-once: a successful scored run creates test-results.json and later
attempts refuse to overwrite it. Run dev freely before freezing; run test exactly once.
"""
import argparse
import hashlib
import json
import math
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from example import pipeline, second_reader  # noqa: E402
from runtime import llm, transport, typesafe  # noqa: E402

CASES = ROOT / "example" / "eval" / "cases.jsonl"
REPLAY = ROOT / "example" / "eval" / "replay.json"
TEST_RESULTS = ROOT / "example" / "eval" / "test-results.json"
PROMPT = ROOT / "example" / "prompts" / "second-reader-v1.json"
SPLIT_SEED = 113
DEV_FRACTION = 0.30
CONFIDENCE_FLOOR = 0.60
MIN_ERRORS = 15
SUCCESS_ERROR_RECALL = 0.50
SUCCESS_MAX_FLAG_RATE = 0.30
SUCCESS_LIFT = 2.0
KILL_LIFT = 1.5
KILL_ERROR_RECALL = 0.30
KILL_FLAG_RATE = 0.40
JEV_USD_PER_MILLION_INPUT_TOKENS = 0.042
EVALUATION_PASS1_MAX_TOKENS = "1200"

# The reader and the harness must flag on the same floor, or the card and the report
# would describe two different experiments.
assert CONFIDENCE_FLOOR == second_reader.CONFIDENCE_FLOOR


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases():
    return [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line]


def split_cases(cases):
    groups = defaultdict(list)
    for case in cases:
        groups[case["gold"]].append(case)
    rng = random.Random(SPLIT_SEED)
    dev, test = [], []
    for label in sorted(groups):
        group = sorted(groups[label], key=lambda item: item["id"])
        rng.shuffle(group)
        cut = round(len(group) * DEV_FRACTION)
        dev.extend(group[:cut])
        test.extend(group[cut:])
    return sorted(dev, key=lambda item: item["id"]), sorted(test, key=lambda item: item["id"])


def to_messages(case):
    return [transport.Message(f"{case['id']}-{index}", entry["text"],
            f"2026-01-01T00:{index:02d}:00", entry["from"] == "them")
            for index, entry in enumerate(case["messages"])]


def wilson(successes, total, z=1.96):
    if not total:
        return [None, None]
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denominator
    return [centre - half, centre + half]


def divide(numerator, denominator):
    return numerator / denominator if denominator else None


def confusion(rows, key):
    result = defaultdict(Counter)
    for row in rows:
        result[row["gold"]][row[key]] += 1
    return {gold: {str(prediction): count for prediction, count in
                   sorted(counts.items(), key=lambda item: str(item[0]))}
            for gold, counts in sorted(result.items())}


def decide(error_count, recall, flag_rate, lift):
    if error_count < MIN_ERRORS or lift is None:
        return "inconclusive"
    if recall >= SUCCESS_ERROR_RECALL and flag_rate <= SUCCESS_MAX_FLAG_RATE and lift >= SUCCESS_LIFT:
        return "success"
    if lift < KILL_LIFT or recall < KILL_ERROR_RECALL or flag_rate > KILL_FLAG_RATE:
        return "kill"
    return "marginal"


def evaluate(cases, record_replay):
    os.environ.setdefault("PASS1_MAX_TOKENS", EVALUATION_PASS1_MAX_TOKENS)
    replay = json.loads(REPLAY.read_text(encoding="utf-8")) if REPLAY.exists() else {}
    classifier_usage = []
    llm.USAGE_HANDLER = lambda prefix, usage: classifier_usage.append(usage) if prefix == "PASS1" else None
    rows = []
    for case in cases:
        messages = to_messages(case)
        classifier = pipeline.classify(transport.render(messages))
        state = second_reader.build_state(messages)
        questions = {second_reader.QUESTION_ID: second_reader.question()}
        body = typesafe.request_body(state, questions)
        response = typesafe.ask(state, questions)
        if record_replay:
            replay[typesafe.request_key(body)] = response
        answer = response["answers"][second_reader.QUESTION_ID]
        jev = answer["choice"]
        confidence = float(answer["confidence"])
        pipeline_intent = classifier.get("intent")
        causes = []
        if jev != pipeline_intent:
            causes.append("disagreement")
        if jev == "unclear":
            causes.append("unclear")
        if confidence < CONFIDENCE_FLOOR:
            causes.append("low_confidence")
        rows.append({"id": case["id"], "gold": case["gold"], "priority_rule": case.get("priority_rule"),
                     "pipeline": pipeline_intent, "jev": jev, "confidence": confidence,
                     "flagged": bool(causes), "flag_causes": causes,
                     "jev_input_tokens": response.get("usage", {}).get("input_tokens", 0)})
    llm.USAGE_HANDLER = None
    if record_replay:
        REPLAY.write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summarise(rows, classifier_usage)


def summarise(rows, classifier_usage):
    errors = [row for row in rows if row["pipeline"] != row["gold"]]
    flagged = [row for row in rows if row["flagged"]]
    caught = [row for row in errors if row["flagged"]]
    unflagged = [row for row in rows if not row["flagged"]]
    error_recall = divide(len(caught), len(errors))
    flag_rate = divide(len(flagged), len(rows))
    error_if_flagged = divide(len(caught), len(flagged))
    error_if_unflagged = divide(len(errors) - len(caught), len(unflagged))
    if error_if_flagged is None:
        lift = None
    elif error_if_unflagged:
        lift = error_if_flagged / error_if_unflagged
    else:
        # Every error was flagged: the ratio is unbounded, which clears any lift bar.
        lift = math.inf if error_if_flagged else None
    tokens = [row["jev_input_tokens"] for row in rows]
    causes = Counter(cause for row in rows for cause in row["flag_causes"])
    return {
        "n": len(rows), "pipeline_errors": len(errors), "flags": len(flagged),
        "caught_errors": len(caught), "error_recall": error_recall,
        "error_recall_wilson_95": wilson(len(caught), len(errors)),
        "flag_rate": flag_rate, "flag_rate_wilson_95": wilson(len(flagged), len(rows)),
        "lift": lift, "pipeline_accuracy": divide(len(rows) - len(errors), len(rows)),
        "jev_accuracy": divide(sum(row["jev"] == row["gold"] for row in rows), len(rows)),
        "pipeline_confusion": confusion(rows, "pipeline"), "jev_confusion": confusion(rows, "jev"),
        "flag_causes": dict(sorted(causes.items())),
        "jev_input_tokens_mean": sum(tokens) / len(tokens), "jev_input_tokens_max": max(tokens),
        "jev_cost_per_1000_messages_usd": (sum(tokens) / len(tokens)) * 1000 / 1_000_000 * JEV_USD_PER_MILLION_INPUT_TOKENS,
        "classifier_usage": classifier_usage,
        "decision": decide(len(errors), error_recall, flag_rate, lift),
        "rows": rows,
    }


def record_demo():
    from example.demo import SEED
    replay = json.loads(REPLAY.read_text(encoding="utf-8")) if REPLAY.exists() else {}
    for messages in SEED.values():
        state = second_reader.build_state(messages)
        questions = {second_reader.QUESTION_ID: second_reader.question()}
        body = typesafe.request_body(state, questions)
        replay[typesafe.request_key(body)] = typesafe.ask(state, questions)
    REPLAY.write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"recorded": len(SEED), "replay_entries": len(replay)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=("demo", "dev", "test"), required=True)
    parser.add_argument("--record-replay", action="store_true")
    parser.add_argument("--summary", action="store_true", help="omit per-case rows from stdout")
    args = parser.parse_args()
    if args.split == "demo":
        print(json.dumps(record_demo(), sort_keys=True))
        return
    dev, test = split_cases(load_cases())
    chosen = dev if args.split == "dev" else test
    if args.split == "test" and TEST_RESULTS.exists():
        raise SystemExit("refusing a second scored test run: test-results.json already exists")
    provider, model, _ = llm.resolve("PASS1")
    if args.split == "test" and provider not in ("openai", "azure"):
        # The keyword stub is too weak a baseline for the scored run to mean anything.
        raise SystemExit("the scored run needs PASS1_PROVIDER=openai or azure")
    result = evaluate(chosen, args.record_replay)
    result["split"] = args.split
    result["pass1_provider"], result["pass1_model"] = provider, model
    result["jev_model"] = typesafe.DEFAULT_MODEL
    result["cases_sha256"] = sha256(CASES)
    result["prompt_sha256"] = sha256(PROMPT)
    output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.split == "test":
        TEST_RESULTS.write_text(output, encoding="utf-8")
    if args.summary:
        result.pop("rows", None)
        output = json.dumps(result, indent=2, sort_keys=True) + "\n"
    print(output, end="")


if __name__ == "__main__":
    main()
