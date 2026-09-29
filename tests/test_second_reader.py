import contextlib
import copy
import io
import json
import logging
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from example import pipeline, second_reader, stub_domain
from runtime import surface, transport, typesafe


def message(text="Sounds good, how much is it?", inbound=True, at="2026-01-01T00:00:00"):
    return transport.Message("m", text, at, inbound)


def answer(choice="question_cost", confidence=0.9, tokens=321):
    options = list(pipeline.ROUTES) + ["unclear"]
    probabilities = {name: 0.0 for name in options}
    probabilities[choice] = confidence
    probabilities["unclear" if choice != "unclear" else "interested"] = 1.0 - confidence
    return {"model": "jev-1.13.0", "answers": {"intent": {
        "type": "choice", "choice": choice, "probabilities": probabilities,
        "confidence": confidence}}, "usage": {"input_tokens": tokens, "output_tokens": 20}}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class OfflineBehaviour(unittest.TestCase):
    def setUp(self):
        stub_domain.install()

    def test_no_key_disables_reader_and_opens_no_socket(self):
        tr = transport.MemoryTransport({"s": [message()]})
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "urllib.request.urlopen", side_effect=AssertionError("network opened")):
            out = pipeline.process("s", tr)
        self.assertNotIn("second_reader", out)
        self.assertEqual(out["intent"], "question_cost")

    def test_demo_seeds_keep_todays_result_without_key(self):
        from example.demo import SEED
        # The result keys as they were before the second reader existed.
        draft_keys = {"no_draft", "subject_id", "intent", "confidence", "flags", "reasoning",
                      "route", "arm", "conversation", "text", "warnings"}
        terminal_keys = {"no_draft", "reason", "intent", "classifier"}
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "urllib.request.urlopen", side_effect=AssertionError("network opened")):
            for subject, messages in SEED.items():
                out = pipeline.process(subject, transport.MemoryTransport({subject: messages}))
                self.assertEqual(set(out), terminal_keys if out["no_draft"] else draft_keys)
                if out["no_draft"]:
                    self.assertEqual(out["reason"], f"terminal intent '{out['intent']}'; no reply needed")

    def test_switch_off_wins_over_a_key(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k", "SECOND_READER": "off"},
                             clear=True):
            self.assertFalse(typesafe.enabled())
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}, clear=True):
            self.assertTrue(typesafe.enabled())


class Client(unittest.TestCase):
    def test_request_shape_is_pinned_and_has_one_stable_choice(self):
        captured = {}

        def opener(req, timeout):
            captured["body"] = json.loads(req.data)
            return Response(answer())

        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "sentinel"}, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=opener):
            typesafe.ask({"newest_inbound": "x"}, {"intent": second_reader.question()})
        body = captured["body"]
        self.assertEqual(body["model"], "jev-1.13.0")
        self.assertEqual(set(body), {"state", "model", "questions"})
        self.assertEqual(list(body["questions"]), ["intent"])
        self.assertEqual(body["questions"]["intent"]["type"], "choice")

    def test_key_never_appears_for_401_422_or_timeout(self):
        sentinel = "SECRET-SENTINEL-KEY"
        failures = [
            urllib.error.HTTPError(typesafe.ENDPOINT, code, "bad", {}, io.BytesIO(b"bad"))
            for code in (401, 422)
        ] + [TimeoutError("timed out")]
        for failure in failures:
            records = []
            handler = logging.Handler()
            handler.emit = lambda record: records.append(record.getMessage())
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": sentinel}, clear=True), \
                        mock.patch("urllib.request.urlopen", side_effect=failure), \
                        mock.patch("time.sleep"):
                    result = second_reader.read([message()], "question_cost")
            finally:
                root.removeHandler(handler)
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": sentinel}, clear=True), \
                    mock.patch("urllib.request.urlopen", side_effect=failure), \
                    mock.patch("time.sleep"):
                with self.assertRaises(RuntimeError) as raised:
                    typesafe.ask({"newest_inbound": "x"}, {"intent": second_reader.question()})
            error = raised.exception
            chain = " ".join(repr(e) for e in (error, error.__cause__, error.__context__) if e)
            card = surface.build_card({"text": "t", "intent": "question_cost",
                                       "second_reader": result}, "r")
            rendered = json.dumps(result) + " ".join(records) + chain + json.dumps(card)
            self.assertNotIn(sentinel, rendered)
            self.assertEqual(result["status"], "unavailable")

    def test_retries_transient_but_not_validation_errors(self):
        def http(code):
            return urllib.error.HTTPError(typesafe.ENDPOINT, code, "x", {}, io.BytesIO(b""))

        for code, calls in ((529, 4), (429, 4), (408, 4), (401, 1), (422, 1)):
            opener = mock.Mock(side_effect=http(code))
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "k"}, clear=True), \
                    mock.patch("urllib.request.urlopen", opener), mock.patch("time.sleep"):
                with self.assertRaises(RuntimeError):
                    typesafe.ask({}, {"intent": second_reader.question()})
            self.assertEqual(opener.call_count, calls, code)

    def test_replay_is_stable_and_uses_no_network(self):
        state = second_reader.build_state([message()])
        questions = {"intent": second_reader.question()}
        key = typesafe.request_key(typesafe.request_body(state, questions))
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "replay.json"
            replay.write_text(json.dumps({key: answer()}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"TYPESAFE_REPLAY": str(replay)}, clear=True), \
                    mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
                first = second_reader.read([message()], "question_cost")
                second = second_reader.read([message()], "question_cost")
        self.assertEqual(first, second)


    def test_demo_from_replay_is_identical_twice_with_no_network(self):
        from example import demo
        # Alex asks about cost; the recorded second reading disagrees, so the card says so.
        replies = {"alex": answer("interested", 0.7), "sam": answer("referral", 0.9),
                   "robin": answer("acknowledgement", 0.95)}
        questions = {second_reader.QUESTION_ID: second_reader.question()}
        recorded = {typesafe.request_key(typesafe.request_body(
            second_reader.build_state(messages), questions)): replies[subject]
            for subject, messages in demo.SEED.items()}
        with tempfile.TemporaryDirectory() as directory:
            replay = Path(directory) / "replay.json"
            replay.write_text(json.dumps(recorded), encoding="utf-8")
            outputs = []
            for _ in range(2):
                buffer = io.StringIO()
                # A fresh copy per run: the in-memory transport appends sent replies to the
                # seed threads, which would make the second run a different conversation.
                with mock.patch.object(demo, "SEED", copy.deepcopy(demo.SEED)), \
                        mock.patch.dict(os.environ, {"TYPESAFE_REPLAY": str(replay)}, clear=True), \
                        mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
                        contextlib.redirect_stdout(buffer):
                    demo.main()
                outputs.append("\n".join(line for line in buffer.getvalue().splitlines()
                                         if "demo.db" not in line))
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("Second reader: interested (p=0.70); pipeline said question_cost", outputs[0])
        self.assertEqual(outputs[0].count("Second reader"), 1)


class DomainAdapter(unittest.TestCase):
    def test_taxonomy_matches_routes_plus_unclear(self):
        self.assertEqual(set(second_reader.question()["criteria"]),
                         set(pipeline.ROUTES) | {"unclear"})

    def test_state_contains_no_timestamps(self):
        state = second_reader.build_state([message(at="2040-12-31T23:59:00")])
        self.assertNotIn("2040", json.dumps(state))
        self.assertEqual(state["thread"], [{"from": "them", "text": message().text}])

    def test_flag_logic(self):
        cases = [
            (answer("question_cost", 0.9), "question_cost", False),
            (answer("interested", 0.9), "question_cost", True),
            (answer("unclear", 0.9), "question_cost", True),
            (answer("question_cost", 0.59), "question_cost", True),
        ]
        for payload, intent, flagged in cases:
            with mock.patch("runtime.typesafe.ask", return_value=payload):
                self.assertEqual(second_reader.read([message()], intent)["flagged"], flagged)

    def test_failure_is_advisory_and_card_is_built(self):
        stub_domain.install()
        tr = transport.MemoryTransport({"s": [message()]})
        with mock.patch("runtime.typesafe.enabled", return_value=True), \
                mock.patch("runtime.typesafe.ask", side_effect=TimeoutError()):
            out = pipeline.process("s", tr)
        self.assertFalse(out["no_draft"])
        card = surface.build_card(out, "r")
        self.assertIn("Second reader unavailable", json.dumps(card))

    def test_card_line_only_when_flagged_and_minimal_card_is_unchanged(self):
        base = {"text": "hello", "intent": "question_cost", "subject_id": "s"}
        plain_minimal = surface.build_minimal_card(base, "r")
        agreed = dict(base, second_reader={"status": "ok", "flagged": False})
        flagged = dict(base, second_reader={"status": "ok", "flagged": True,
            "choice": "interested", "probabilities": {"interested": 0.81}})
        self.assertNotIn("Second reader", json.dumps(surface.build_card(agreed, "r")))
        self.assertIn("Second reader: interested (p=0.81); pipeline said question_cost",
                      json.dumps(surface.build_card(flagged, "r")))
        self.assertEqual(plain_minimal, surface.build_minimal_card(flagged, "r"))

    def test_terminal_disagreement_is_recorded(self):
        stub_domain.install()
        tr = transport.MemoryTransport({"s": [message("Thanks, one more thing: how much?")]})
        with mock.patch("runtime.typesafe.enabled", return_value=True), \
                mock.patch("example.second_reader.read", return_value={
                    "status": "ok", "choice": "question_cost", "disagrees": True,
                    "flagged": True, "probabilities": {"question_cost": 0.9},
                    "confidence": 0.9, "input_tokens": 10}), \
                mock.patch("example.pipeline.classify", return_value={
                    "intent": "acknowledgement", "confidence": "high", "flags": {}}):
            out = pipeline.process("s", tr)
        self.assertTrue(out["no_draft"])
        self.assertIn("second reader said 'question_cost'", out["reason"])


class Harness(unittest.TestCase):
    def setUp(self):
        import importlib
        self.m = importlib.import_module("tools.measure_second_reader")

    def rows(self, spec):
        """spec: (count, pipeline_wrong, flagged)."""
        out = []
        for count, wrong, flagged in spec:
            for _ in range(count):
                out.append({"id": str(len(out)), "gold": "interested", "priority_rule": None,
                            "pipeline": "hard_no" if wrong else "interested",
                            "jev": "interested", "confidence": 0.9, "flagged": flagged,
                            "flag_causes": ["disagreement"] if flagged else [],
                            "jev_input_tokens": 1000})
        return out

    def test_metrics_and_success(self):
        # 20 errors, 15 flagged; 25 flags in 100 cases.
        r = self.m.summarise(self.rows([(15, True, True), (5, True, False),
                                        (10, False, True), (70, False, False)]), [])
        self.assertEqual((r["pipeline_errors"], r["flags"]), (20, 25))
        self.assertAlmostEqual(r["error_recall"], 0.75)
        self.assertAlmostEqual(r["flag_rate"], 0.25)
        self.assertAlmostEqual(r["lift"], (15 / 25) / (5 / 75))
        self.assertEqual(r["decision"], "success")
        self.assertAlmostEqual(r["jev_cost_per_1000_messages_usd"], 0.042)

    def test_decision_rules(self):
        self.assertEqual(self.m.decide(14, 1.0, 0.1, 9.0), "inconclusive")
        self.assertEqual(self.m.decide(20, 0.5, 0.3, 2.0), "success")
        self.assertEqual(self.m.decide(20, 0.6, 0.2, 1.4), "kill")
        self.assertEqual(self.m.decide(20, 0.29, 0.2, 3.0), "kill")
        self.assertEqual(self.m.decide(20, 0.6, 0.41, 3.0), "kill")
        self.assertEqual(self.m.decide(20, 0.4, 0.2, 1.8), "marginal")

    def test_every_error_flagged_is_unbounded_lift(self):
        r = self.m.summarise(self.rows([(16, True, True), (4, False, True),
                                        (80, False, False)]), [])
        self.assertEqual(r["lift"], float("inf"))
        self.assertEqual(r["decision"], "success")

    def test_wilson_interval(self):
        low, high = self.m.wilson(15, 20)
        self.assertAlmostEqual(low, 0.5313, places=3)
        self.assertAlmostEqual(high, 0.8881, places=3)

    def test_split_is_stratified_and_fixed(self):
        cases = self.m.load_cases()
        dev, test = self.m.split_cases(cases)
        self.assertEqual((len(cases), len(dev), len(test)), (264, 77, 187))
        self.assertFalse({c["id"] for c in dev} & {c["id"] for c in test})
        self.assertEqual(self.m.split_cases(cases), (dev, test))
        per_label = {}
        for case in dev:
            per_label[case["gold"]] = per_label.get(case["gold"], 0) + 1
        self.assertEqual(set(per_label.values()), {7})


if __name__ == "__main__":
    unittest.main()
