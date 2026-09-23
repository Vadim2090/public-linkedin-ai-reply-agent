"""The example domain: a support inbox.

Deliberately generic. The runtime this sits on was built for a different domain entirely,
and the point of the split is that everything under `runtime/` did not have to change to
come here. What changed is this file, four prompt files, and nothing else.

TWO PASSES, NOT ONE. Classification is separated from generation because in the
single-pass version almost every failure came from misreading the state of the
conversation, and that error then cascaded into a confidently wrong reply. Separating them
buys three things: a cheap model can do the classification, a terminal state can
short-circuit the expensive call entirely, and the card can show the classifier's own
reasoning - which is what lets a reviewer tell a routing bug from a wording bug. Those get
fixed in different places, so conflating them wastes the correction.

ROUTING IS CODE. The intent decides the route, and that decision is a dict here rather
than a paragraph in the prompt. A prompt asked to both choose a route and write in it will
occasionally do neither well, and when it goes wrong there is nothing to unit test.
"""
import hashlib
import json
import re
from pathlib import Path

from runtime import llm, transport

PROMPTS = Path(__file__).resolve().parent / "prompts"

#: Intent -> (route, word cap). A table, not prose in a prompt: this is testable, and it
#: is the thing a reviewer's correction most often means to change.
ROUTES = {
    "question":        ("answer_then_offer", 120),
    "bug_report":      ("acknowledge_and_escalate", 90),
    "billing":         ("answer_then_offer", 110),
    "feature_request": ("thank_and_log", 70),
    "complaint":       ("acknowledge_and_escalate", 90),
    "acknowledgement": ("terminal", 0),
    "spam":            ("terminal", 0),
}

#: Routes that end the conversation. Reaching one short-circuits the generator entirely:
#: no second model call, no card, no human asked to approve a message nobody needs.
TERMINAL = {"terminal"}


def load_prompt(name):
    """Read a prompt from disk on EVERY call, never at import.

    This is not a micro-optimisation, it is the deploy story. Editing a prompt file changes
    behaviour on the next message with no restart and no release. In the system this came
    from, the prompt used to live in a hosted automation tool's text box, so the live
    version and the repository silently diverged for months and nobody could say which
    text had produced a given reply.
    """
    return (PROMPTS / name).read_text(encoding="utf-8")


def claim_key(subject_id, messages):
    """Content-derived, so a retry is a duplicate but a genuinely new message is not.

    Keying on the subject alone would swallow their next message. Keying on a random id
    would defeat the guard. Keying on the newest inbound message's content does both jobs.
    """
    msgs = transport.ordered(messages)
    newest = msgs[-1].text if msgs else ""
    return f"{subject_id}:{hashlib.sha1(newest.encode()).hexdigest()[:16]}"


def classify(conversation_text):
    """Pass 1. Cheap model, JSON mode, no prose."""
    raw = llm.chat(
        [{"role": "system", "content": load_prompt("classify-v1.txt")},
         {"role": "user", "content": conversation_text}],
        prefix="PASS1", json_mode=True, max_tokens=500, temperature=0.2)
    try:
        out = json.loads(raw)
    except json.JSONDecodeError:
        # A classifier that returns junk must not take the request down, and must not be
        # silently treated as a valid low-confidence answer either.
        return {"intent": None, "confidence": "low", "flags": {},
                "reasoning": "classifier returned unparseable output", "parse_failed": True}
    out.setdefault("flags", {})
    return out


def generate(conversation_text, classifier_json, route, word_cap):
    """Pass 2. The route and the cap are passed IN, already decided."""
    system = load_prompt("generate-v1.txt")
    user = (f"ROUTE: {route}\nWORD LIMIT: {word_cap}\n"
            f"CLASSIFIER: {json.dumps(classifier_json)}\n\nCONVERSATION:\n{conversation_text}")
    return llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                    prefix="PASS2", max_tokens=800, temperature=0.3)


def correct(conversation_text, draft, critique, policy_text):
    """Pass 3, and it is a THIRD pass rather than a re-run of the second.

    The generator applies templates. Correcting a draft against a critique is reasoning
    over constraints, and a template-applier asked to do it will satisfy the critique by
    breaking a different rule. So: its own prompt, and it must NAME THE RULE IT VIOLATED
    before emitting text - which makes the named rule the cluster tag, confirmed for free
    on a click the reviewer was already making.

    It runs on roughly one card in eight, so a more capable tier is affordable here in a
    way it is not on the generator.
    """
    system = load_prompt("correct-v1.txt")
    user = (f"REVIEWER FEEDBACK: {critique}\n\nDRAFT:\n{draft}\n\n"
            f"CONVERSATION:\n{conversation_text}\n\nPOLICY:\n{policy_text}")
    raw = llm.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                   prefix="PASS3", json_mode=True, max_tokens=1200, temperature=0.2)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"violated": "", "policy_quote": "", "reply": draft,
                "explanation": "corrector returned unparseable output"}


# ----------------------------------------------------------- deterministic chokepoint

_EM_DASH = re.compile(r"\s*[—–]\s*")
_MULTISPACE = re.compile(r"[ \t]{2,}")


def normalise(text):
    """Every draft passes through here. THE POINT OF THIS FUNCTION IS ITS EXISTENCE.

    A prompt rule broken twice belongs in code. "Do not use em-dashes" was in the prompt
    for months, in capitals, and the model kept using them - because a style instruction
    competes with everything else in a long prompt and sometimes loses. Moving it here
    bought zero failures instead of fewer failures.

    Note what it does NOT do: hyphens inside compound terms survive. A naive strip breaks
    product names and version numbers, which is why this ran as a prompt rule for so long.
    The precision is the reason it works in code and not in prose.
    """
    text = _EM_DASH.sub(", ", text or "")
    text = _MULTISPACE.sub(" ", text)
    return text.strip()


def word_count(text):
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def process(subject_id, tr: transport.Transport):
    """Fetch, decide whether to reply at all, classify, route, generate, clean.

    Returns a draft dict, or a dict with `no_draft` and a reason. The reason is always
    populated: "we skipped deliberately" and "nothing happened" must never look the same
    from the outside, and the second is how a waiting customer gets lost.
    """
    messages = tr.fetch_thread(subject_id)
    if not messages:
        return {"no_draft": True, "reason": "no messages for this subject"}
    if not transport.should_reply(messages):
        return {"no_draft": True, "reason": "our message is the most recent; not our turn"}

    conversation = transport.render(messages)
    classifier = classify(conversation)
    intent = classifier.get("intent")
    route, cap = ROUTES.get(intent, ("answer_then_offer", 120))

    if route in TERMINAL:
        return {"no_draft": True, "reason": f"terminal intent '{intent}'; no reply needed",
                "intent": intent, "classifier": classifier}

    text = normalise(generate(conversation, classifier, route, cap))
    over = word_count(text) - cap

    return {
        "no_draft": False,
        "subject_id": subject_id,
        "intent": intent,
        "confidence": classifier.get("confidence"),
        "flags": classifier.get("flags", {}),
        "reasoning": classifier.get("reasoning", ""),
        "route": route,
        "conversation": conversation,
        "text": text,
        # Surfaced on the card rather than enforced silently. A hard truncation mid-sentence
        # is worse than a slightly long message a human can see and judge.
        "warnings": ([f"{over} words over the {cap}-word limit"] if over > 0 else []),
    }
