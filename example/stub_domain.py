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
    ("bug_report", r"\b(fail(s|ed|ing)?|error|crash|broken|not work|doesn'?t work|"
                   r"nothing (happens|downloads)|502|500|timed? out)\b"),
    ("billing",    r"\b(refund|charg(e|ed|es)|invoice|billing|payment|subscription|plan|price)\b"),
    ("feature_request", r"\b(would be (great|nice)|feature request|could you add|wish (it|you)|"
                        r"any plans to|roadmap)\b"),
    ("complaint",  r"\b(unacceptable|disappointed|frustrat(ed|ing)|third time|still waiting|"
                   r"terrible|awful)\b"),
    ("acknowledgement", r"^\W*(thanks?|thank you|got it|ok(ay)?|perfect|great|cheers)\b[\s\S]{0,30}$"),
    ("spam",       r"\b(crypto|seo services|guest post|backlink|investment opportunity)\b"),
    ("question",   r"\?"),
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

    for intent, pattern in RULES:
        if re.search(pattern, low, re.I):
            return json.dumps({
                "intent": intent,
                "confidence": "high" if intent != "question" else "medium",
                "flags": {
                    "angry": bool(re.search(r"\b(unacceptable|furious|terrible|awful)\b", low)),
                    "asks_for_human": bool(re.search(r"\b(a human|real person|speak to someone)\b", low)),
                    "mentions_refund": "refund" in low,
                    "blocked_on_us": bool(re.search(r"\b(blocked|can'?t proceed|waiting on you)\b", low)),
                    "already_answered": False,
                },
                "reasoning": f"offline stub: matched the {intent} pattern in the newest "
                             f"inbound message. Not a model.",
            })
    return json.dumps({"intent": "question", "confidence": "low", "flags": {},
                       "reasoning": "offline stub: nothing matched, defaulting to question."})


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
