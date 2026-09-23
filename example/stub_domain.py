"""A deterministic stand-in for the model, for THIS domain only.

Installed into `runtime.llm.STUB_HANDLER` so the demo and the tests produce sensible,
reproducible output with no API key. The runtime itself stays free of any of this: support
intents are the example's business, not the runtime's.

It is a keyword table and it is not pretending otherwise. Its job is to make the state
machine observable offline, not to classify anything well.
"""
import json
import re

RULES = [
    ("out_of_office",   r"\b(out of (the )?office|annual leave|on holiday|away until|"
                        r"auto(matic)?[- ]?reply)\b"),
    ("referral",        r"\b(speak to|talk to|reach out to|you want|better person|"
                        r"forward(ed|ing)? (this|you)|cc'?ing|introduce you)\b"),
    ("hard_no",         r"\b(not interested|stop (contacting|messaging)|remove me|"
                        r"unsubscribe|do not contact|no thanks?\b.*\bdon'?t)\b"),
    ("already_using",   r"\b(already (use|using|have|work with)|we built|in[- ]house|"
                        r"current (provider|vendor|supplier))\b"),
    ("question_cost",   r"\b(how much|price|pricing|cost|budget|per seat|contract|"
                        r"commercials?)\b"),
    ("question_us",     r"\b(who are you|what is \w+\?|how did you (find|get)|why (me|us)|"
                        r"where did you get)\b"),
    ("not_now",         r"\b(next (quarter|year|month)|after (the )?(new year|summer)|"
                        r"revisit|circle back|not right now|bad timing|q[1-4]\b)"),
    ("not_relevant",    r"\b(not my (remit|area|department)|wrong person|i don'?t handle|"
                        r"nothing to do with me)\b"),
    ("acknowledgement", r"^\W*(thanks?|thank you|got it|ok(ay)?|perfect|great|cheers|"
                        r"noted)\b[\s\S]{0,30}$"),
    ("question_product", r"\b(how does it work|what does it do|tell me more|what exactly|"
                         r"how would (it|that) work|integrat)\w*"),
    ("interested",      r"\b(interested|sounds (good|interesting)|happy to|let'?s (talk|do|"
                        r"chat)|send (it |me |them )?(over|through|across)|yes\b|keen)\b"),
]

def handler(messages, json_mode):
    """Return a JSON answer for this domain, or None to let the generic stub answer."""
    if not json_mode:
        return None
    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    user = "\n".join(m.get("content", "") for m in messages if m.get("role") != "system")

    if "policy_quote" in system:
        return _correction(user)
    if "anchor" in system and "replacement" in system:
        return _proposal(user)
    # Only the newest inbound line decides the intent. Matching the whole rendered thread
    # lets an old message from last week set today's route, which is a real bug worth not
    # reproducing even in a stub.
    newest = _newest_inbound(user)
    low = newest.lower()

    whole = user.lower()
    for intent, pattern in RULES:
        if re.search(pattern, low, re.I):
            return json.dumps({
                "intent": intent,
                "confidence": "high" if intent != "question_product" else "medium",
                "flags": {
                    # Flags are read from the WHOLE thread, not the newest line: they decide
                    # whether a reply may repeat something already sent.
                    "offer_already_presented": bool(re.search(r"https?://", whole)),
                    "booking_link_shared": "example.com/book" in whole,
                    "closing_already_sent": bool(re.search(r"\bno (rush|pressure)\b", whole)),
                    "asks_who_we_are": bool(re.search(r"\b(who are you|what is this)\b", low)),
                    "explicit_ask": "?" in low,
                    "names_a_colleague": intent == "referral",
                    "wrong_person": intent == "not_relevant",
                    "already_using_someone": intent == "already_using",
                },
                "reasoning": f"offline stub: matched the {intent} pattern in the newest "
                             f"inbound message. Not a model.",
            })
    return json.dumps({"intent": "question_product", "confidence": "low", "flags": {},
                       "reasoning": "offline stub: nothing matched, defaulting to "
                                    "question_product."})


def _policy_lines(user):
    """The numbered rules out of the POLICY block the caller appended."""
    block = user.split("POLICY:", 1)[-1]
    return [ln.rstrip() for ln in block.splitlines() if re.match(r"^\s*\d+\.\s+\S", ln)]


def _correction(user):
    """Pick the rule the critique is about and COPY ITS LINE, exactly.

    Copying rather than naming is the whole point of the binding: see runtime/policy.py.
    A stub that invented a plausible label instead would demonstrate the bug, not the fix.
    """
    critique = user.split("REVIEWER FEEDBACK:", 1)[-1].split("\n", 1)[0].lower()
    lines = _policy_lines(user)
    best, best_score = (lines[0] if lines else ""), -1
    # Stem-ish prefix matching, so "promises" in the critique finds "PROMISE" in the rule.
    # Exact substring matching sent an early version of this to the wrong rule, which is a
    # small live demonstration of why the binding is checked against the document at all.
    keywords = {w[:5] for w in re.findall(r"[a-z]{4,}", critique)} - {"this", "have", "been", "that"}
    for line in lines:
        low = line.lower()
        score = sum(1 for stem in keywords if stem in low)
        if score > best_score:
            best, best_score = line, score
    draft = user.split("DRAFT:", 1)[-1].split("\n\nCONVERSATION:", 1)[0].strip()
    return json.dumps({
        "violated": "",                       # left blank on purpose: the quote identifies it
        "policy_quote": best.strip(),
        "explanation": "offline stub: matched the reviewer's words against the policy lines.",
        "reply": re.sub(r"(?i)\bby friday\b", "as soon as the team has looked at it", draft),
        "confidence": "medium",
    })


def _proposal(user):
    """A conservative stand-in for the weekly prompt-edit proposal: refuse.

    `not_a_prompt_problem` is the honest default, and the refusal is the behaviour worth
    demonstrating. A stub that always produced an edit would model the failure mode the
    two-occurrence threshold exists to prevent.
    """
    return json.dumps({
        "anchor": "", "replacement": "", "verdict": "not_a_prompt_problem",
        "rationale": "offline stub: it does not read the cluster, so it refuses rather "
                     "than inventing an edit to a rule that may already be correct.",
    })


def _newest_inbound(rendered):
    """The last `<- THEM` block of a rendered conversation, or the raw text."""
    parts = re.split(r"^\[.*?\]\s*(<- THEM|-> US)\s*$", rendered, flags=re.M)
    if len(parts) < 3:
        return rendered
    # parts = [pre, marker, body, marker, body, ...]
    for i in range(len(parts) - 2, 0, -2):
        if parts[i] == "<- THEM":
            return parts[i + 1]
    return rendered


def install():
    from runtime import llm
    llm.STUB_HANDLER = handler
