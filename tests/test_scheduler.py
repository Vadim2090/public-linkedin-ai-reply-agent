"""The scheduled engine: who gets nudged, who does not, and why the 'why' is stored.

The suppression tests matter more than the send tests. An engine that nudges the wrong
person is embarrassing once; an engine whose suppression silently eats everyone is invisible
until someone asks why it has gone quiet.
"""
import tempfile
import time
import unittest
from pathlib import Path

from runtime import scheduler, store, transport


def msg(text, inbound, at):
    return transport.Message("m", text, at, inbound)


def days_ago(n, now):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - n * 86400))


class Scan(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        store.init()
        scheduler.init_skips()
        self.now = time.time()
        self.tr = transport.MemoryTransport({
            # quiet for a week, we spoke last: eligible
            "quiet": [msg("here is the thing you asked for", False, days_ago(7, self.now))],
            # they answered: not a silence
            "answered": [msg("ours", False, days_ago(9, self.now)),
                         msg("theirs", True, days_ago(8, self.now))],
            # yesterday: too recent
            "fresh": [msg("ours", False, days_ago(1, self.now))],
            "empty": [],
        })
        self.rules = scheduler.standard_rules(min_quiet_days=3.0, max_touches=2,
                                              dnc={"blocked"})

    def _scan(self, subjects=None):
        return scheduler.scan(subjects or list(self.tr.threads) + ["blocked"], self.tr,
                              self.rules,
                              generate=lambda s, t, n: {"text": f"nudge {n}", "intent": "nudge"},
                              now=self.now)

    def test_only_the_genuinely_quiet_get_a_card(self):
        created = self._scan()
        self.assertEqual(len(created), 1)
        self.assertEqual(store.get_approval(created[0])["subject_id"], "quiet")

    def test_every_suppressed_subject_leaves_a_reason(self):
        self._scan()
        reasons = dict(scheduler.skip_report(0))
        self.assertEqual(reasons.get("they_spoke_last"), 1)
        self.assertEqual(reasons.get("too_recent"), 1)
        self.assertEqual(reasons.get("empty_thread"), 1)
        self.assertEqual(reasons.get("do_not_contact"), 1)

    def test_no_subject_falls_out_of_a_scan_silently(self):
        subjects = list(self.tr.threads) + ["blocked"]
        created = self._scan(subjects)
        skipped = sum(n for _, n in scheduler.skip_report(0))
        self.assertEqual(len(created) + skipped, len(subjects),
                         "a subject that vanishes from a scan is indistinguishable from "
                         "one that was never scanned")

    def test_the_first_matching_rule_is_the_recorded_one(self):
        # "blocked" would also fail other rules; do-not-contact is checked first and is
        # the cheapest, so it is the one that should be recorded.
        scheduler.scan(["blocked"], self.tr, self.rules,
                       generate=lambda s, t, n: {"text": "x"}, now=self.now)
        self.assertEqual(scheduler.skip_report(0), [("do_not_contact", 1)])

    def test_an_empty_thread_is_not_reported_as_too_recent(self):
        # Ordering regression. An empty conversation has a quiet-time of zero, so a rule
        # list with `too_recent` above `empty_thread` files every history-less subject
        # under the wrong reason: correct behaviour, lying explanation.
        scheduler.scan(["empty"], self.tr, self.rules,
                       generate=lambda s, t, n: {"text": "x"}, now=self.now)
        self.assertEqual(scheduler.skip_report(0), [("empty_thread", 1)])

    def test_a_declined_draft_is_still_a_recorded_skip(self):
        scheduler.scan(["quiet"], self.tr, self.rules,
                       generate=lambda s, t, n: None, now=self.now)
        self.assertEqual(dict(scheduler.skip_report(0)).get("no_draft"), 1)

    def test_a_queued_card_suppresses_a_second(self):
        self._scan(["quiet"])
        scheduler.scan(["quiet"], self.tr, self.rules,
                       generate=lambda s, t, n: {"text": "x"}, now=self.now)
        self.assertEqual(dict(scheduler.skip_report(0)).get("already_queued"), 1,
                         "one subject must never have two cards waiting on one human")

    def test_the_touch_cap_holds(self):
        for _ in range(2):
            rid = store.save_approval({"text": "n"}, kind="nudge", subject_id="quiet")
            store.set_status(rid, "approved")
        scheduler.scan(["quiet"], self.tr, self.rules,
                       generate=lambda s, t, n: {"text": "x"}, now=self.now)
        self.assertEqual(dict(scheduler.skip_report(0)).get("touch_cap"), 1)

    def test_the_touch_number_travels_on_the_card(self):
        rid = store.save_approval({"text": "n"}, kind="nudge", subject_id="quiet")
        store.set_status(rid, "approved")
        created = scheduler.scan(["quiet"], self.tr, self.rules,
                                 generate=lambda s, t, n: {"text": f"nudge {n}"}, now=self.now)
        self.assertEqual(store.get_approval(created[0])["data"]["touch"], 2)


class ClickTimeRecheck(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        store.init()
        self.now = time.time()

    def test_a_reply_since_the_card_was_written_refuses_the_send(self):
        tr = transport.MemoryTransport({"s": [
            msg("ours", False, days_ago(5, self.now)),
            msg("they answered while the card waited", True, days_ago(1, self.now))]})
        refusal = scheduler.pre_send_recheck({"subject_id": "s"}, tr)
        self.assertIn("replied since", refusal)

    def test_an_unchanged_world_permits_the_send(self):
        tr = transport.MemoryTransport({"s": [msg("ours", False, days_ago(5, self.now))]})
        self.assertEqual(scheduler.pre_send_recheck({"subject_id": "s"}, tr), "")

    def test_a_new_do_not_contact_entry_refuses(self):
        tr = transport.MemoryTransport({"s": [msg("ours", False, days_ago(5, self.now))]})
        self.assertIn("do-not-contact",
                      scheduler.pre_send_recheck({"subject_id": "s"}, tr, dnc={"s"}))

    def test_an_unreadable_conversation_fails_closed(self):
        class Broken:
            def fetch_thread(self, s):
                raise RuntimeError("upstream 503")

        refusal = scheduler.pre_send_recheck({"subject_id": "s"}, Broken())
        self.assertIn("refusing rather than guessing", refusal,
                      "an unreachable system must never read as 'nothing has changed'")

    def test_a_card_with_no_subject_refuses(self):
        self.assertTrue(scheduler.pre_send_recheck({}, transport.MemoryTransport({})))


if __name__ == "__main__":
    unittest.main()
