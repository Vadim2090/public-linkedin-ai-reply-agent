"""Run the whole loop end to end, offline, with no keys and no chat workspace.

    python -m example.demo

Seeds three conversations, drafts a reply to each, prints the cards to the terminal, then
drives one of each verdict - Approve, Fix then Approve, Decline - and prints the weekly
header the review ritual would open with.

With `LLM_PROVIDER=stub` (the default) every model call is a deterministic local function,
so this is reproducible and free. Point `PASS1_PROVIDER` / `PASS2_PROVIDER` at azure or
openai and the same code path makes real calls.
"""
import tempfile
import time
from pathlib import Path

from runtime import approval, policy, review, store, surface
from runtime.transport import Message, MemoryTransport
from . import pipeline, stub_domain

# Three inbound replies to a cold outreach message. Invented, but the SHAPES are the ones
# that actually arrive: a question, a redirect to a colleague, and a closing courtesy.
SEED = {
    "alex": [
        Message("m0", "Hi Alex, we work with operations teams on manual reporting. Worth a "
                      "short conversation?", "2026-03-01T08:00:00", inbound=False),
        Message("m1", "Sounds interesting, how much does it cost?",
                "2026-03-01T09:00:00", inbound=True),
    ],
    "sam": [
        Message("m2", "Hi Sam, quick note about reporting workload on your ops team.",
                "2026-03-01T08:00:00", inbound=False),
        Message("m3", "Not my remit I'm afraid, you want whoever runs ops reporting.",
                "2026-03-01T09:05:00", inbound=True),
    ],
    "robin": [
        Message("m4", "Here is the overview I mentioned.", "2026-03-01T08:00:00", inbound=False),
        Message("m5", "Thanks, got it.", "2026-03-01T08:30:00", inbound=True),
    ],
}


def build_registry(send_enabled=lambda: True, approvers=lambda: ["reviewer-1"]):
    """One default policy, one extra engine, to show what registering a second costs."""
    reply = approval.Policy(
        kind="reply", label="replies", send_lock_name="REPLY_SEND_ENABLED",
        approvers=approvers, send_allowed=send_enabled, allow_fix=True)
    reg = approval.Registry(reply)
    # A second engine is one registration and no change to any control flow. Note
    # allow_fix=False: correcting a nudge against the REPLY policy would quietly rewrite it
    # into a reply, which is a different message to a different person's situation.
    reg.register(approval.Policy(
        kind="nudge", label="nudges", send_lock_name="NUDGE_SEND_ENABLED",
        approvers=approvers, send_allowed=send_enabled, allow_fix=False,
        sent_suffix=lambda a: f" (nudge {a.get('touch', 1)})"))
    return reg


def main():
    stub_domain.install()          # offline stand-in for the model; see example/stub_domain.py
    db = Path(tempfile.mkdtemp()) / "demo.db"
    store.DB_PATH = db
    store.init()

    tr = MemoryTransport(SEED)
    console = surface.ConsolePoster()
    registry = build_registry()
    policy_path = pipeline.PROMPTS / "generate-v1.txt"

    print("\n### 1. Three inbound messages arrive\n")
    cards = {}
    for subject in SEED:
        # The claim is taken BEFORE any model call. Two workers on the same inbound must
        # not both pay for a draft, let alone both post a card.
        key = pipeline.claim_key(subject, tr.fetch_thread(subject))
        if not store.claim(key):
            print(f"  {subject}: already claimed, skipping")
            continue
        draft = pipeline.process(subject, tr)
        if draft.get("no_draft"):
            print(f"  {subject}: no card - {draft['reason']}")
            continue
        rid = store.save_approval(draft, kind="reply", subject_id=subject)
        ok, _, _ = surface.post_card(console, draft, rid)
        if ok:
            store.set_card_version(rid, 1)
            cards[subject] = rid

    print("\n### 2. Alex's draft is approved as written\n")
    rid = cards["alex"]
    pol = registry.for_approval(store.get_approval(rid))
    if approval.may_click(pol, "reviewer-1") and pol.send_allowed():
        if store.set_status(rid, "approved", by="reviewer-1", expect_version=1):
            tr.send("alex", store.get_approval(rid)["data"]["text"])
            print("  sent. A second click on the same card:")
            print("  ->", store.set_status(rid, "approved", by="reviewer-1", expect_version=1),
                  "(the conditional UPDATE matched nothing, so nothing was sent twice)")

    print("\n### 3. Sam's draft is wrong, so the reviewer presses Fix\n")
    rid = cards.get("sam")
    if rid is None:
        print("  (sam produced no card, so there is nothing to fix)")
        return
    appr = store.get_approval(rid)
    critique = "It promises an outcome nobody gave us."
    result = pipeline.correct(appr["data"]["conversation"], appr["data"]["text"],
                             critique, policy_path.read_text(encoding="utf-8"))
    bound = policy.bind(result.get("violated"), result.get("policy_quote"), policy_path)
    version = store.bump_version(rid, pipeline.normalise(result.get("reply") or appr["data"]["text"]))
    store.set_card_version(rid, version)
    store.record_revision(rid, version, kind="fix", subject_id="sam",
                          reviewer_reason=critique, reviewer_id="reviewer-1",
                          draft_before=appr["data"]["text"],
                          draft_after=store.get_approval(rid)["data"]["text"],
                          intent=appr["data"].get("intent"),
                          violated=bound["violated"], policy_quote=bound["policy_quote"],
                          tag_resolved=bound["tag_resolved"], explanation=result.get("explanation"))
    print(f"  corrected to v{version}; tag bound to the policy file: "
          f"{bool(bound['tag_resolved'])} ({bound['detail']})")
    print("  a click on the OLD v1 card now:")
    print("  ->", store.set_status(rid, "approved", by="reviewer-1", expect_version=1),
          "(refused: the button carried v1 and the row is on v2)")
    store.set_status(rid, "approved", by="reviewer-1", expect_version=version)
    tr.send("sam", store.get_approval(rid)["data"]["text"])
    print("  approved at v2 and sent.")

    print("\n### 4. A card nobody wants\n")
    rid = store.save_approval({"text": "Glad that was useful.", "intent": "acknowledgement"},
                              kind="reply", subject_id="robin")
    store.set_status(rid, "declined", by="reviewer-1", expect_version=1)
    store.record_revision(rid, 1, kind="decline", subject_id="robin",
                          reviewer_reason="They said thanks and nothing else. No reply is needed.",
                          reviewer_id="reviewer-1", violated=store.TAG_DECLINE_PLAIN)
    print("  declined with a required reason. This is an ENTRY GATE signal, not a wording")
    print("  one, so the weekly pass reports it separately and never proposes a prompt edit.")

    print("\n### 5. An engine that failed to load\n")
    pol = registry.for_kind("recovery")
    print(f"  policy for an unregistered kind: send_allowed={pol.send_allowed()}, "
          f"allow_fix={pol.allow_fix}")
    print("  the card stays closable, but it cannot send. An import error must never")
    print("  silently bypass an engine's own off switch.")

    print("\n### 6. The weekly header\n")
    h = review.header(time.time() - 7 * 86400)
    for k, v in h.items():
        print(f"  {k:>16}: {v}")
    print("\n  Interventions become prompt-edit proposals only at two occurrences of the")
    print("  same tag. One correction is an anecdote.")
    print(f"\n  state: {db}\n  messages actually sent: {len(tr.sent)}\n")


if __name__ == "__main__":
    main()
