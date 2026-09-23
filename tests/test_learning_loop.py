"""Binding a finding to the policy document, and the threshold that turns findings into
proposals.

The binding tests are the ones that matter. A wrong binding is invisible downstream: the
weekly batch looks identical whether a finding landed on the right rule or not, so nothing
catches it except a test that asks the question directly.
"""
import tempfile
import time
import unittest
from pathlib import Path

from runtime import policy, review, store

POLICY = """\
GLOBAL RULES (MANDATORY)

1. ANSWER WHAT THEY ASKED, AND ONLY THAT. Do not preview adjacent steps.
2. NEVER PROMISE A DATE, a fix, or an outcome you were not given in the conversation.
3. NEVER INVENT A FACT about the product or an account.

REVIEW FIXES

1. PLAIN LANGUAGE. No corporate apology formulas; say the specific thing.
2. NEVER PROMISE A DATE, a fix, or an outcome you were not given in the conversation.
"""


class Binding(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "policy.txt"
        self.path.write_text(POLICY, encoding="utf-8")

    def test_exact_quote_binds_and_the_document_supplies_the_label(self):
        quote = "3. NEVER INVENT A FACT about the product or an account."
        label, detail = policy.resolve(quote, POLICY)
        self.assertEqual(label, "GLOBAL RULES 3")
        self.assertIn("exact", detail)

    def test_two_lists_numbered_from_one_are_disambiguated_by_section(self):
        label, _ = policy.resolve(
            "1. PLAIN LANGUAGE. No corporate apology formulas; say the specific thing.", POLICY)
        self.assertEqual(label, "REVIEW FIXES 1",
                         "the same number in a different section is a different rule")

    def test_a_quote_matching_two_rules_refuses_rather_than_guessing(self):
        # This line is deliberately identical in both sections. Guessing here is exactly
        # how a week of findings landed on the wrong paragraph in production.
        label, detail = policy.resolve(
            "2. NEVER PROMISE A DATE, a fix, or an outcome you were not given in the conversation.",
            POLICY)
        self.assertIsNone(label)
        self.assertIn("ambiguous", detail)

    def test_a_paraphrase_does_not_bind(self):
        label, detail = policy.resolve(
            "Never promise the customer a delivery date that nobody gave you, ever.", POLICY)
        self.assertIsNone(label)
        self.assertIn("not found", detail)

    def test_whitespace_and_rewrapping_survive(self):
        quote = "3.   NEVER INVENT A FACT   about the product\n   or an account."
        label, _ = policy.resolve(quote, POLICY)
        self.assertEqual(label, "GLOBAL RULES 3",
                         "editors rewrap files; that must not break the binding")

    def test_a_short_quote_is_not_evidence(self):
        label, detail = policy.resolve("NEVER.", POLICY)
        self.assertIsNone(label)
        self.assertIn("under the", detail)

    def test_the_document_overrules_a_wrong_label_and_says_so(self):
        out = policy.bind("GLOBAL RULE 16",
                          "3. NEVER INVENT A FACT about the product or an account.", self.path)
        self.assertEqual(out["violated"], "GLOBAL RULES 3")
        self.assertEqual(out["violated_raw"], "GLOBAL RULE 16")
        self.assertTrue(out["tag_resolved"])
        self.assertTrue(out["relabelled"],
                        "how often the document overrules the model is itself a metric")

    def test_an_unbindable_finding_keeps_the_raw_label_and_is_flagged(self):
        out = policy.bind("GLOBAL RULE 99", "something I made up", self.path)
        self.assertEqual(out["violated"], "GLOBAL RULE 99")
        self.assertFalse(out["tag_resolved"],
                         "unbound findings must be countable, not silently mixed in")


class Clustering(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        store.init()

    def _revision(self, violated, reason="the draft did the wrong thing", kind="fix"):
        rid = store.save_approval({"text": "d"})
        store.record_revision(rid, 1, kind=kind, violated=violated, reviewer_reason=reason)

    def test_one_occurrence_is_an_anecdote(self):
        self._revision("GLOBAL RULES 2")
        self.assertEqual(review.cluster(store.revisions_since(0)), {},
                         "acting on n=1 is how a prompt reaches 700 lines")

    def test_two_occurrences_are_a_pattern(self):
        self._revision("GLOBAL RULES 2")
        self._revision("GLOBAL RULES 2")
        self.assertIn("GLOBAL RULES 2", review.cluster(store.revisions_since(0)))

    def test_plain_declines_never_become_prompt_proposals(self):
        self._revision(store.TAG_DECLINE_PLAIN, kind="decline")
        self._revision(store.TAG_DECLINE_PLAIN, kind="decline")
        self.assertEqual(review.cluster(store.revisions_since(0)), {},
                         "a draft nobody wanted is an entry-gate signal, not a wording one")

    def test_unbound_findings_are_excluded_from_clustering(self):
        self._revision("unbound")
        self._revision("unbound")
        self.assertEqual(review.cluster(store.revisions_since(0)), {})

    def test_a_cluster_with_no_reviewer_words_is_skipped_not_invented(self):
        self._revision("GLOBAL RULES 2", reason="")
        self._revision("GLOBAL RULES 2", reason="")
        made, skipped = review.build_candidates(
            0, propose_edit=lambda *a: self.fail("must not ask for an edit with no evidence"),
            policy_path="unused.txt")
        self.assertEqual(made, [])
        self.assertEqual(skipped[0][1], "no reviewer words recorded")

    def test_evidence_is_the_reviewers_own_words(self):
        self._revision("GLOBAL RULES 2", reason="it invented a Friday deadline")
        self._revision("GLOBAL RULES 2", reason="promised a date again")
        ev = review.evidence(review.cluster(store.revisions_since(0))["GLOBAL RULES 2"])
        self.assertEqual(ev[0]["words"], "it invented a Friday deadline")


class ApplyingAnEdit(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "policy.txt"
        self.path.write_text(POLICY, encoding="utf-8")

    def test_an_exact_anchor_applies(self):
        ok, detail = review.apply_edit(
            {"anchor": "3. NEVER INVENT A FACT about the product or an account.",
             "replacement": "3. NEVER INVENT A FACT about anything at all."}, self.path)
        self.assertTrue(ok, detail)
        self.assertIn("about anything at all", self.path.read_text())

    def test_a_stale_anchor_refuses(self):
        ok, detail = review.apply_edit(
            {"anchor": "a rule that was edited away last week", "replacement": "x"}, self.path)
        self.assertFalse(ok)
        self.assertIn("policy changed", detail)

    def test_an_ambiguous_anchor_refuses(self):
        ok, detail = review.apply_edit(
            {"anchor": "NEVER PROMISE A DATE", "replacement": "x"}, self.path)
        self.assertFalse(ok)
        self.assertIn("ambiguous", detail)
        self.assertIn("NEVER PROMISE A DATE", self.path.read_text(),
                      "a refused edit must leave the file untouched")


class Header(unittest.TestCase):
    def setUp(self):
        store.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        store.init()

    def test_the_header_separates_unwanted_drafts_from_wording_problems(self):
        for status, fix in (("approved", False), ("approved", True),
                            ("declined", False), ("declined", False)):
            rid = store.save_approval({"text": "d"})
            if fix:
                store.bump_version(rid, "d2")
                store.record_revision(rid, 2, kind="fix", violated="GLOBAL RULES 2",
                                      tag_resolved=1, reviewer_reason="w")
            store.set_status(rid, status, expect_version=2 if fix else 1)
        h = review.header(time.time() - 3600)
        self.assertEqual(h["decided"], 4)
        self.assertEqual(h["unwanted"], 2)
        self.assertEqual(h["unwanted_share"], 50.0)
        self.assertEqual(h["bound"], 1)
        self.assertEqual(h["unbound"], 0)


if __name__ == "__main__":
    unittest.main()
