"""The gate itself: who may click, what can send, and what happens when the card will not
render.

Two independent layers are being tested here and they are often confused. A valid signature
proves the REQUEST came from the platform. It says nothing about whether THIS PERSON may
approve. Both have to hold.
"""
import time
import unittest

from example import pipeline, stub_domain
from runtime import approval, surface, transport

SECRET = "test-signing-secret"


def sign(body: bytes, ts: int, secret=SECRET):
    import hashlib
    import hmac
    base = b"v0:" + str(ts).encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class RequestAuthenticity(unittest.TestCase):
    def test_a_good_signature_verifies(self):
        body, ts = b"payload=1", int(time.time())
        self.assertTrue(surface.verify_signature(body, str(ts), sign(body, ts), SECRET))

    def test_a_tampered_body_fails(self):
        ts = int(time.time())
        self.assertFalse(
            surface.verify_signature(b"payload=2", str(ts), sign(b"payload=1", ts), SECRET))

    def test_an_old_request_fails_even_with_a_valid_signature(self):
        body = b"payload=1"
        old = int(time.time()) - surface.REPLAY_WINDOW - 5
        self.assertFalse(surface.verify_signature(body, str(old), sign(body, old), SECRET),
                         "a captured request must not be replayable forever")

    def test_no_secret_configured_refuses_everything(self):
        body, ts = b"payload=1", int(time.time())
        self.assertFalse(surface.verify_signature(body, str(ts), sign(body, ts), secret=""),
                         "the permissive version of this line is a stranger approving your "
                         "messages, and it looks identical in every log until they do")

    def test_a_malformed_timestamp_fails_closed(self):
        self.assertFalse(surface.verify_signature(b"x", "not-a-number", "v0=deadbeef", SECRET))


class ClickAuthorisation(unittest.TestCase):
    def _policy(self, approvers):
        return approval.Policy(kind="reply", label="replies", send_lock_name="X",
                               approvers=lambda: approvers, send_allowed=lambda: True)

    def test_an_approver_may_click(self):
        self.assertTrue(approval.may_click(self._policy(["u1"]), "u1"))

    def test_anyone_else_may_not(self):
        self.assertFalse(approval.may_click(self._policy(["u1"]), "u2"))

    def test_an_empty_approver_list_authorises_nobody(self):
        self.assertFalse(approval.may_click(self._policy([]), "u1"),
                         "an unconfigured deployment must approve nothing, not everything")


class PolicyRegistry(unittest.TestCase):
    def setUp(self):
        self.default = approval.Policy(
            kind="reply", label="replies", send_lock_name="REPLY_SEND_ENABLED",
            approvers=lambda: ["u1"], send_allowed=lambda: True)
        self.registry = approval.Registry(self.default)

    def test_the_default_kind_resolves(self):
        self.assertIs(self.registry.for_kind(None), self.default)
        self.assertIs(self.registry.for_kind("reply"), self.default)

    def test_an_unregistered_kind_cannot_send(self):
        p = self.registry.for_kind("recovery")
        self.assertFalse(p.send_allowed(),
                         "an import error must never bypass an engine's own off switch")
        self.assertFalse(p.allow_fix)

    def test_an_unregistered_kind_can_still_be_closed(self):
        p = self.registry.for_kind("recovery")
        self.assertTrue(approval.may_click(p, "u1"),
                        "somebody has to be able to close the card; only sending is refused")

    def test_the_send_lock_is_read_at_click_time(self):
        state = {"open": False}
        reg = approval.Registry(approval.Policy(
            kind="reply", label="replies", send_lock_name="X",
            approvers=lambda: ["u1"], send_allowed=lambda: state["open"]))
        p = reg.for_kind("reply")
        self.assertFalse(p.send_allowed())
        state["open"] = True
        self.assertTrue(p.send_allowed(),
                        "a card can sit for days; a snapshotted boolean freezes the lock")

    def test_registering_an_engine_is_one_call(self):
        self.registry.register(approval.Policy(
            kind="nudge", label="nudges", send_lock_name="NUDGE_SEND_ENABLED",
            approvers=lambda: ["u1"], send_allowed=lambda: False, allow_fix=False))
        self.assertEqual(self.registry.loaded_kinds(), ["nudge", "reply"])
        self.assertFalse(self.registry.for_kind("nudge").send_allowed())


class CardFallback(unittest.TestCase):
    DRAFT = {"text": "hello", "intent": "question", "subject_id": "s1", "conversation": "c"}

    def test_the_full_card_is_used_when_it_posts(self):
        calls = []

        def poster(blocks, text):
            calls.append(blocks)
            return True, "chan", "ts"

        ok, _, _ = surface.post_card(poster, self.DRAFT, "rid")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)

    def test_a_rejected_full_card_falls_back_to_minimal(self):
        calls = []

        def poster(blocks, text):
            calls.append(blocks)
            return (len(calls) > 1), "chan", "ts"      # first attempt rejected

        ok, _, _ = surface.post_card(poster, self.DRAFT, "rid")
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2)

    def test_both_cards_failing_still_reaches_a_human_as_plain_text(self):
        calls = []

        def poster(blocks, text):
            calls.append(blocks)
            return (blocks is None), "chan", "ts"      # only plain text succeeds

        ok, _, _ = surface.post_card(poster, self.DRAFT, "rid")
        self.assertTrue(ok)
        self.assertIsNone(calls[-1], "the last resort carries no blocks")

    def test_total_failure_is_reported_not_swallowed(self):
        ok, channel, ts = surface.post_card(lambda b, t: (False, None, None), self.DRAFT, "rid")
        self.assertFalse(ok, "'nothing needed review' and 'nobody was asked' must differ")

    def test_an_exception_from_the_poster_still_falls_back(self):
        calls = []

        def poster(blocks, text):
            calls.append(blocks)
            if len(calls) == 1:
                raise RuntimeError("platform 500")
            return True, "chan", "ts"

        ok, _, _ = surface.post_card(poster, self.DRAFT, "rid")
        self.assertTrue(ok)

    def test_buttons_carry_the_version(self):
        blocks = surface.build_card(self.DRAFT, "rid123", version=3)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        self.assertTrue(all(e["value"] == "rid123:3" for e in actions["elements"]))
        self.assertEqual(surface.parse_button_value("rid123:3"), ("rid123", 3))

    def test_fix_is_absent_when_the_policy_forbids_it(self):
        blocks = surface.build_card(self.DRAFT, "rid", allow_fix=False)
        actions = [b for b in blocks if b["type"] == "actions"][0]
        self.assertNotIn("fix", [e["action_id"] for e in actions["elements"]])


class EntryGate(unittest.TestCase):
    def _msg(self, text, inbound, at="2026-01-01T00:00:00"):
        return transport.Message("m", text, at, inbound)

    def test_we_reply_only_when_they_spoke_last(self):
        self.assertTrue(transport.should_reply([self._msg("hi", True)]))

    def test_we_do_not_reply_to_ourselves(self):
        self.assertFalse(transport.should_reply([
            self._msg("their question", True, "2026-01-01T00:00:00"),
            self._msg("our answer", False, "2026-01-01T00:01:00")]),
            "the single check that ended a whole class of 'why did it answer itself'")

    def test_ordering_is_by_timestamp_not_by_api_order(self):
        newest_first = [self._msg("ours", False, "2026-01-02T00:00:00"),
                        self._msg("theirs", True, "2026-01-01T00:00:00")]
        self.assertFalse(transport.should_reply(newest_first),
                         "paged endpoints return newest first; trusting that inverts the answer")

    def test_an_empty_thread_is_not_our_turn(self):
        self.assertFalse(transport.should_reply([]))


class DeterministicChokepoint(unittest.TestCase):
    def test_em_dashes_are_removed(self):
        self.assertNotIn("—", pipeline.normalise("a sentence — and another"))

    def test_hyphens_inside_words_survive(self):
        for term in ("pay-on-success", "read-only", "v1-beta"):
            self.assertIn(term, pipeline.normalise(f"our {term} option"),
                          "a naive strip breaks product names, which is why this stayed "
                          "a prompt rule for far too long")

    def test_it_is_idempotent(self):
        once = pipeline.normalise("a — b")
        self.assertEqual(once, pipeline.normalise(once))


class Routing(unittest.TestCase):
    def setUp(self):
        stub_domain.install()

    def test_a_terminal_intent_never_reaches_the_generator(self):
        for text in ("Thanks, got it.", "Out of office until 5 May."):
            tr = transport.MemoryTransport({"s": [
                transport.Message("m", text, "2026-01-01T00:00:00", True)]})
            out = pipeline.process("s", tr)
            self.assertTrue(out["no_draft"], text)
            self.assertIn("terminal", out["reason"])

    def test_the_arm_is_decided_in_code_and_is_stable(self):
        first = pipeline.resolve_arm("subject-42", "no links here")
        self.assertIn(first, pipeline.ARMS)
        self.assertEqual(first, pipeline.resolve_arm("subject-42", "different text entirely"),
                         "the same subject must land on the same arm forever, with nothing "
                         "stored: that is what survives a restart and a replay")

    def test_a_link_already_sent_beats_the_hash(self):
        convo = f"we shared {pipeline.VARIABLES['FORM_URL']} last week"
        self.assertEqual(pipeline.resolve_arm("subject-42", convo), "form",
                         "a real link beats wording; inferring the arm from the prose is "
                         "what used to flip subjects onto the wrong arm")

    def test_a_cross_arm_link_is_flagged_not_silently_sent(self):
        out = dict(arm="call", text=f"here you go {pipeline.VARIABLES['FORM_URL']}")
        wrong = pipeline.VARIABLES["FORM_URL" if out["arm"] == "call" else "BOOKING_LINK"]
        self.assertIn(wrong, out["text"])

    def test_an_unknown_intent_falls_back_rather_than_crashing(self):
        route, cap = pipeline.ROUTES.get("something_new", ("answer_then_offer", 110))
        self.assertEqual(route, "answer_then_offer")

    def test_every_intent_has_a_route(self):
        import json as _json
        names = set(_json.loads(stub_domain.handler(
            [{"role": "system", "content": pipeline.load_prompt("classify-v1.txt")},
             {"role": "user", "content": "x"}], True)).keys())
        self.assertTrue(names)
        for intent in pipeline.ROUTES:
            self.assertIn(intent, pipeline.load_prompt("classify-v1.txt"),
                          f"{intent} is routed but not defined in the classifier prompt")

    def test_a_skip_always_carries_a_reason(self):
        out = pipeline.process("nobody", transport.MemoryTransport({}))
        self.assertTrue(out["no_draft"])
        self.assertTrue(out["reason"],
                        "'we skipped deliberately' and 'nothing happened' must never look alike")


if __name__ == "__main__":
    unittest.main()
