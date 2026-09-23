"""The weekly pass: turn a week of human corrections into at most a few proposed prompt
edits, and report whether the previous ones worked.

THE CADENCE HAS A REASON. One correction is an anecdote; two of the same tag is a pattern.
At roughly seventy interventions a week, a DAILY pass sees ten, so almost every tag would
be a singleton - and acting on singletons is exactly how a prompt grows to seven hundred
lines of accreted special cases, each one a reasonable response to one bad Tuesday. Weekly
is the filter. A same-day trigger exists only for a tag that spikes, so a live regression
is not left running for six days.

WHAT IS DELIBERATELY NOT A PROMPT PROBLEM. A card declined with no attempt to correct it
means the system should not have drafted at all. That is the entry gate, not the wording,
and no edit to a generator prompt touches it. Those are EXCLUDED from candidate building
and REPORTED SEPARATELY, because in production they were 18% of all decided cards - the
single largest source of wasted human attention, and a ritual that only ever proposes
wording changes would never have surfaced it.
"""
import hashlib
import time
from collections import defaultdict

from . import store

#: Two occurrences before a tag may become a proposal. The threshold is the whole design.
MIN_RECURRENCE = 2


def cluster(rows, min_recurrence=MIN_RECURRENCE):
    """Group interventions by the rule they broke.

    Clusters on `violated`, which by then has been resolved against the policy document
    (see `policy.py`). Clustering on a label the model typed is the bug that module exists
    to prevent, and it fails silently: the batch looks identical either way.
    """
    by_tag = defaultdict(list)
    for r in rows:
        tag = (r.get("violated") or "").strip()
        if not tag or tag == "unbound":
            continue
        # A decline with no correction attempt is an entry-gate signal, not a wording one.
        if tag in (store.TAG_DECLINE_PLAIN,):
            continue
        by_tag[tag].append(r)
    return {t: rs for t, rs in by_tag.items() if len(rs) >= min_recurrence}


def evidence(rows, n=3):
    """Three interventions in the reviewer's OWN WORDS.

    Not a summary. The weekly pass's contract calls the reviewer's words the ground truth,
    so a card that shows a generated paraphrase instead is asking a human to approve an
    edit based on a retelling of what they said.

    THE REASON FIELD MUST BE REQUIRED UPSTREAM. When it was optional, 108 reviewers out of
    108 left it empty, and the proposal pass - having nothing to read - invented a pattern
    from the tag name alone and put it up for approval. Optional means empty.
    """
    out = []
    for r in rows[:n]:
        words = (r.get("reviewer_reason") or "").strip()
        if words:
            out.append({"words": words, "before": r.get("draft_before"), "after": r.get("draft_after")})
    return out


def build_candidates(since_ts, propose_edit, policy_path, week=None):
    """Cluster the week, ask for one edit per recurring tag, store each as a candidate.

    `propose_edit(tag, rows, evidence, policy_text) -> {anchor, replacement, rationale}`
    is injected rather than imported: the proposal step is a model call in production and
    a stub in tests, and the review loop should not care which.

    The edit is stored as an exact anchor -> replacement PAIR. See the note in
    `store.init`: a diff can apply in the wrong place, an exact string cannot.
    """
    week = week or time.strftime("%G-W%V")
    rows = store.revisions_since(since_ts)
    made, skipped = [], []
    for tag, tag_rows in sorted(cluster(rows).items(), key=lambda kv: -len(kv[1])):
        ev = evidence(tag_rows)
        if not ev:
            # No reviewer words means no ground truth. Refusing here is the whole lesson
            # from the empty-reason-field incident.
            skipped.append((tag, "no reviewer words recorded"))
            continue
        proposal = propose_edit(tag, tag_rows, ev, policy_path)
        if not proposal or not proposal.get("anchor"):
            skipped.append((tag, proposal.get("reason") if proposal else "no proposal"))
            continue
        cand_id = hashlib.sha1(f"{week}:{tag}".encode()).hexdigest()[:16]
        store.save_candidate(
            cand_id, week, tag, len(tag_rows),
            evidence=ev, prompt_file=str(policy_path),
            anchor=proposal["anchor"], replacement=proposal["replacement"],
            rationale=proposal.get("rationale", ""))
        made.append(store.get_candidate(cand_id))
    return made, skipped


def apply_edit(candidate, policy_path):
    """Apply an approved edit by exact string replacement, or refuse.

    The anchor must appear EXACTLY ONCE. Zero means the prompt changed since the proposal
    was written, which is the staleness guard doing its job. More than one means the edit
    is ambiguous and would land in an arbitrary place. Both refuse, and a refusal here is
    a safe outcome: the tag simply returns next week.
    """
    from pathlib import Path
    p = Path(policy_path)
    text = p.read_text(encoding="utf-8")
    anchor = candidate["anchor"]
    n = text.count(anchor)
    if n == 0:
        return False, "anchor not found: the policy changed since this was proposed"
    if n > 1:
        return False, f"anchor appears {n} times: ambiguous, refusing rather than guessing"
    p.write_text(text.replace(anchor, candidate["replacement"]), encoding="utf-8")
    return True, "applied"


def effect(tag, applied_at, window_seconds, now=None):
    """Did last week's edit work?

    Same-length windows either side of the edit, and NORMALISED BY VOLUME. The raw counts
    are not comparable: in production this queue went from 37 to 565 items a week, so an
    unchanged failure RATE shows up as many more occurrences and reads as "worse", while a
    genuine improvement can read as "back in the queue". The rate decides the verdict;
    both numbers are reported so a reader can see which one moved.
    """
    now = now or time.time()
    rows_before = store.revisions_since(applied_at - window_seconds)
    before = sum(1 for r in rows_before
                 if r.get("violated") == tag and r["created_at"] < applied_at)
    after = sum(1 for r in rows_before
                if r.get("violated") == tag and r["created_at"] >= applied_at)
    n_before = store.acceptance_stats(applied_at - window_seconds)["decided"] or 1
    n_after = store.acceptance_stats(applied_at)["decided"] or 1
    rate_before = before / n_before
    rate_after = after / n_after
    mature = (now - applied_at) >= window_seconds
    if not mature:
        verdict = "pending"
    elif after == 0:
        verdict = "resolved"
    elif rate_after < rate_before * 0.5:
        verdict = "improved"
    elif rate_after > rate_before * 1.5:
        verdict = "worse"
    else:
        verdict = "unchanged"
    return {"tag": tag, "before": before, "after": after,
            "rate_before": round(rate_before, 4), "rate_after": round(rate_after, 4),
            "verdict": verdict}


def header(since_ts):
    """What the weekly batch leads with.

    THIS EXISTS BECAUSE A RITUAL THAT DOES NOT SHOW ITS OWN EFFECT DIES. A weekly card
    asking for approvals, with no evidence that last week's approvals changed anything, is
    a meeting. The header is the only proof the loop is learning rather than churning.
    """
    acc = store.acceptance_stats(since_ts)
    rows = store.revisions_since(since_ts)
    fixes = [r for r in rows if (r.get("kind") or "") == "fix"]
    bound = sum(1 for r in fixes if r.get("tag_resolved"))
    return {
        **acc,
        # Drafts nobody wanted, as a share. An entry-gate number, reported here precisely
        # because no prompt edit can move it.
        "unwanted_share": round(100.0 * acc["unwanted"] / acc["decided"], 1) if acc["decided"] else None,
        "interventions": len(rows),
        # How often the tag actually bound to a line of the policy. The difference
        # between "fixed" and "believed fixed".
        "bound": bound,
        "unbound": len(fixes) - bound,
    }
