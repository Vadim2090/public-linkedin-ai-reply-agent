"""The three concurrency primitives, and the two ways a card can be acted on twice.

These are the tests worth having. Everything else in this repository is legible by reading
it; a race is not, and each of these cases corresponds to a real incident: a customer
messaged twice in a minute and got two answers, a webhook retried and paid for two drafts,
a reviewer double-clicked a slow button, and a stale card in a channel sent superseded text.
"""
import tempfile
import time
import unittest
from pathlib import Path

from runtime import store


class Base(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        store.init()


class TrailingDebounce(Base):
    def test_second_arrival_replaces_rather_than_queues(self):
        store.queue_inbound("s1", {"n": 1}, delay_seconds=60)
        store.queue_inbound("s1", {"n": 2}, delay_seconds=60)
        with store._conn() as c:
            rows = c.execute("SELECT * FROM pending WHERE subject_id='s1'").fetchall()
        self.assertEqual(len(rows), 1, "two arrivals must collapse to one unit of work")
        self.assertEqual(store.json.loads(rows[0]["payload"])["n"], 2,
                         "the work must run against the LATEST state, not the first")

    def test_timer_moves_forward_on_each_arrival(self):
        store.queue_inbound("s1", {}, delay_seconds=1)
        with store._conn() as c:
            first = c.execute("SELECT fire_at FROM pending").fetchone()["fire_at"]
        store.queue_inbound("s1", {}, delay_seconds=60)
        with store._conn() as c:
            second = c.execute("SELECT fire_at FROM pending").fetchone()["fire_at"]
        self.assertGreater(second, first, "a new message must push the deadline out")

    def test_only_due_rows_come_back(self):
        store.queue_inbound("ready", {}, delay_seconds=-1)
        store.queue_inbound("waiting", {}, delay_seconds=300)
        due = [s for s, _ in store.due_inbounds()]
        self.assertEqual(due, ["ready"])

    def test_subjects_do_not_interfere(self):
        store.queue_inbound("a", {}, delay_seconds=-1)
        store.queue_inbound("b", {}, delay_seconds=-1)
        self.assertEqual(len(store.due_inbounds()), 2,
                         "the debounce is per subject, not global")


class DistributedLock(Base):
    def test_only_one_claim_wins(self):
        results = [store.claim("k"), store.claim("k")]
        self.assertEqual(results, [True, False],
                         "the rowcount check IS the lock; without it both callers proceed")

    def test_different_keys_are_independent(self):
        self.assertTrue(store.claim("k1"))
        self.assertTrue(store.claim("k2"))

    def test_release_reopens_the_key(self):
        store.claim("k")
        store.release_claim("k")
        self.assertTrue(store.claim("k"),
                        "release exists for a FAILED attempt that should be retried")


class OptimisticConcurrency(Base):
    def setUp(self):
        super().setUp()
        self.rid = store.save_approval({"text": "draft one"}, subject_id="s1")

    def test_double_click_cannot_send_twice(self):
        first = store.set_status(self.rid, "approved", by="u", expect_version=1)
        second = store.set_status(self.rid, "approved", by="u", expect_version=1)
        self.assertTrue(first)
        self.assertFalse(second, "the second click must match zero rows, so nothing sends")

    def test_declining_an_approved_card_is_refused(self):
        store.set_status(self.rid, "approved", by="u")
        self.assertFalse(store.set_status(self.rid, "declined", by="u"),
                         "a terminal state is terminal; expect='pending' guards it")

    def test_stale_card_click_is_refused(self):
        version = store.bump_version(self.rid, "draft two")
        self.assertEqual(version, 2)
        self.assertFalse(
            store.set_status(self.rid, "approved", by="u", expect_version=1),
            "a button minted on v1 must not send v2's text")
        self.assertTrue(store.set_status(self.rid, "approved", by="u", expect_version=2))

    def test_correction_keeps_the_original_draft(self):
        store.bump_version(self.rid, "draft two")
        data = store.get_approval(self.rid)["data"]
        self.assertEqual(data["draft_v1"], "draft one",
                         "the rejected draft and the correction are a pair, or neither is useful")
        self.assertEqual(data["text"], "draft two",
                         "`text` is always what the reviewer is looking at")

    def test_second_correction_does_not_overwrite_the_original(self):
        store.bump_version(self.rid, "draft two")
        store.bump_version(self.rid, "draft three")
        self.assertEqual(store.get_approval(self.rid)["data"]["draft_v1"], "draft one")


class CardVisibility(Base):
    def test_a_corrected_card_that_never_posted_is_detectable(self):
        rid = store.save_approval({"text": "one"})
        store.set_card_version(rid, 1)
        store.bump_version(rid, "two")          # the corrected card fails to post
        orphans = [o["request_id"] for o in store.orphaned_cards()]
        self.assertIn(rid, orphans,
                      "'the row is on v2' and 'a human can act on v2' must be distinguishable")
        store.set_card_version(rid, 2)
        self.assertEqual(store.orphaned_cards(), [])


class AcceptanceStats(Base):
    def test_cohort_denominator_counts_every_decided_card(self):
        for text, status, fix in (("a", "approved", False), ("b", "approved", True),
                                  ("c", "declined", False), ("d", "approved", False)):
            rid = store.save_approval({"text": text})
            if fix:
                store.bump_version(rid, text + "!")
            store.set_status(rid, status, expect_version=2 if fix else 1)
        s = store.acceptance_stats(time.time() - 3600)
        self.assertEqual(s["decided"], 4)
        self.assertEqual(s["clean"], 2)
        self.assertEqual(s["fixed"], 1)
        self.assertEqual(s["unwanted"], 1)
        self.assertEqual(s["acceptance"], 50.0)

    def test_pending_cards_are_not_in_the_denominator(self):
        store.save_approval({"text": "still waiting"})
        self.assertEqual(store.acceptance_stats(time.time() - 3600)["decided"], 0,
                         "an undecided card is not evidence either way")


if __name__ == "__main__":
    unittest.main()
