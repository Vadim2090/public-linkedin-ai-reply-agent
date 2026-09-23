"""The example domain: inbound replies to cold outreach on a professional network.

Structurally this is the production system with the business removed. Same shape - a cold
message goes out, someone replies, and the reply has to be read, routed and answered - but
the company, the product, the copy, the figures and the links are placeholders. None of the
real policy ships, and none of it needs to for the engineering to be legible.

WHAT THE SHAPE ACTUALLY IS, and it is not obvious from the outside: this is not a writing
aid. The classifier's verdict is what creates the CRM record downstream, because the tag it
sets is what a webhook filter is keyed on. A missed classification is a lost lead rather
than a reporting gap, and that is the sharpest way to say what a pipeline like this is for.

TWO PASSES, NOT ONE. Classification is separated from generation because in the single-pass
version almost every failure came from misreading the state of the conversation, and that
error then cascaded into a confidently wrong reply. Separating them lets a cheap model
classify, lets a terminal state skip the expensive call entirely, and puts the classifier's
own reasoning on the card - which is what lets a reviewer tell a routing bug from a wording
bug. Those get fixed in different files.

ROUTING IS CODE, AND SO IS THE ARM. A prompt asked to both choose a route and write in it
will occasionally do neither well, and when it goes wrong there is nothing to unit test.
"""
import hashlib
import json
import re
from pathlib import Path

from runtime import llm, transport

PROMPTS = Path(__file__).resolve().parent / "prompts"

#: The placeholder values the generator may use. In production this block held the real
#: price list, the real links and the approved credibility line - which is exactly why the
#: real one cannot ship and this one can. Keeping the STRUCTURE matters: a generator with a
#: variables block cannot invent a figure, because rule 3 tells it nothing outside this
#: block exists.
VARIABLES = {
    "COMPANY": "Northwind",
    "ONE_LINER": "we help operations teams cut manual reporting work",
    "BOOKING_LINK": "https://example.com/book",
    "FORM_URL": "https://example.com/form",
    "CASES_URL": "https://example.com/cases",
}

#: Intent -> (route, word cap). A table, not prose in a prompt: this is testable, diffable,
#: and it is the thing a reviewer's correction most often means to change.
ROUTES = {
    "interested":       ("offer_next_step", 70),
    "question_product": ("answer_then_offer", 110),
    "question_cost":    ("answer_then_offer", 110),
    "question_us":      ("answer_then_offer", 90),
    "not_now":          ("park_warmly", 60),
    "not_relevant":     ("scope_out", 40),
    "already_using":    ("compare_briefly", 80),
    "referral":         ("pick_up_the_referral", 60),
    "hard_no":          ("close_politely", 20),
    "acknowledgement":  ("terminal", 0),
    "out_of_office":    ("terminal", 0),
}

#: Routes that end the conversation. Reaching one short-circuits the generator entirely: no
#: second model call, no card, nobody asked to approve a message nobody needs.
TERMINAL = {"terminal"}

ARMS = ("call", "form")


def load_prompt(name):
    """Read a prompt from disk on EVERY call, never at import.

    This is not a micro-optimisation, it is the deploy story. Editing a prompt file changes
    behaviour on the next message with no restart and no release. In the system this came
    from, the prompt lived in a hosted automation tool's text box for a year, so the live
    version and the repository silently diverged and nobody could say which text had
    produced a given reply.
    """
    return (PROMPTS / name).read_text(encoding="utf-8")


def render_prompt(name, variables=None):
    text = load_prompt(name)
    for key, value in (variables or VARIABLES).items():
        text = text.replace("{{" + key + "}}", value)
    return text


def claim_key(subject_id, messages):
    """Content-derived, so a retry is a duplicate but a genuinely new message is not.

    Keying on the subject alone would swallow their next message. Keying on a random id
    would defeat the guard. Keying on the newest inbound message's content does both jobs.
    """
    msgs = transport.ordered(messages)
    newest = msgs[-1].text if msgs else ""
    return f"{subject_id}:{hashlib.sha1(newest.encode()).hexdigest()[:16]}"


# --------------------------------------------------------------------- arm resolution

_LINK = re.compile(r"https?://\S+")


def resolve_arm(subject_id, conversation_text, variables=None):
    """Which experiment arm this subject is locked to. DETERMINISTIC, and decided here.

    Two rules, in this order, and the order is the whole point.

    1. A LINK ALREADY IN THE THREAD WINS. If we have sent one of the two links, the subject
       is on that arm, full stop. They may have acted on it.
    2. Otherwise, a stable hash of the subject id. Stable means the same subject lands on
       the same arm on every call, forever, without storing anything - which is what makes
       the assignment survive a restart, a replay and a re-read.

    This used to be inferred by the generator from the wording of the thread, and it drifted:
    a message that merely SOUNDED like the call arm flipped a subject who had actually been
    sent a form. A real link beats wording, and reading it is three lines of code.
    """
    v = variables or VARIABLES
    links = set(_LINK.findall(conversation_text or ""))
    for arm, key in (("call", "BOOKING_LINK"), ("form", "FORM_URL")):
        if any(v[key] in link for link in links):
            return arm
    digest = hashlib.sha1(subject_id.encode()).digest()[0]
    return ARMS[digest % len(ARMS)]


# ------------------------------------------------------------------------- the passes

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


def generate(conversation_text, classifier_json, route, arm, word_cap):
    """Pass 2. The route, the arm and the cap are passed IN, already decided."""
    user = (f"ROUTE: {route}\nARM: {arm}\nWORD LIMIT: {word_cap}\n"
            f"CLASSIFIER: {json.dumps(classifier_json)}\n\nCONVERSATION:\n{conversation_text}")
    return llm.chat([{"role": "system", "content": render_prompt("generate-v1.txt")},
                     {"role": "user", "content": user}],
                    prefix="PASS2", max_tokens=800, temperature=0.3)


def correct(conversation_text, draft, critique, policy_text):
    """Pass 3, and it is a THIRD pass rather than a re-run of the second.

    The generator applies templates. Correcting a draft against a critique is reasoning over
    constraints, and a template-applier asked to do it will satisfy the critique by breaking
    a different rule. So: its own prompt, and it must NAME THE RULE IT VIOLATED before
    emitting text - which makes the named rule the cluster tag, confirmed for free on a
    click the reviewer was already making.

    It runs on roughly one card in eight, so a more capable tier is affordable here in a way
    it is not on the generator.
    """
    user = (f"REVIEWER FEEDBACK: {critique}\n\nDRAFT:\n{draft}\n\n"
            f"CONVERSATION:\n{conversation_text}\n\nPOLICY:\n{policy_text}")
    raw = llm.chat([{"role": "system", "content": load_prompt("correct-v1.txt")},
                    {"role": "user", "content": user}],
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

    A prompt rule broken twice belongs in code. "Do not use em-dashes" was in the prompt for
    months, in capitals, and the model kept using them, because a style instruction competes
    with everything else in a long prompt and sometimes loses. Moving it here bought zero
    failures instead of fewer failures.

    Note what it does NOT do: hyphens inside compound terms survive. A naive strip breaks
    product names and version numbers, which is why this ran as a prompt rule for so long.
    The precision is the reason it works in code and not in prose.
    """
    text = _EM_DASH.sub(", ", text or "")
    text = _MULTISPACE.sub(" ", text)
    return text.strip()


def word_count(text):
    return len(re.findall(r"\b[\w'-]+\b", text or ""))


def link_count(text):
    return len(_LINK.findall(text or ""))


def process(subject_id, tr: transport.Transport):
    """Fetch, decide whether to reply at all, classify, route, generate, clean.

    Returns a draft dict, or a dict with `no_draft` and a reason. The reason is ALWAYS
    populated: "we skipped deliberately" and "nothing happened" must never look the same
    from the outside, and the second is how a waiting person gets lost.
    """
    messages = tr.fetch_thread(subject_id)
    if not messages:
        return {"no_draft": True, "reason": "no messages for this subject"}
    if not transport.should_reply(messages):
        return {"no_draft": True, "reason": "our message is the most recent; not our turn"}

    conversation = transport.render(messages)
    classifier = classify(conversation)
    intent = classifier.get("intent")
    route, cap = ROUTES.get(intent, ("answer_then_offer", 110))
    arm = resolve_arm(subject_id, conversation)

    if route in TERMINAL:
        return {"no_draft": True, "reason": f"terminal intent '{intent}'; no reply needed",
                "intent": intent, "classifier": classifier}

    text = normalise(generate(conversation, classifier, route, arm, cap))

    # Guardrails that RIDE ON THE CARD rather than blocking silently. A hard truncation
    # mid-sentence is worse than a slightly long message a human can see and judge, and a
    # silent rejection is worse than both.
    warnings = []
    over = word_count(text) - cap
    if over > 0:
        warnings.append(f"{over} words over the {cap}-word limit")
    if link_count(text) > 1:
        warnings.append(f"{link_count(text)} links; the rule is one")
    wrong_link = VARIABLES["FORM_URL" if arm == "call" else "BOOKING_LINK"]
    if wrong_link in text:
        warnings.append(f"carries the other arm's link while locked to '{arm}'")

    return {
        "no_draft": False,
        "subject_id": subject_id,
        "intent": intent,
        "confidence": classifier.get("confidence"),
        "flags": classifier.get("flags", {}),
        "reasoning": classifier.get("reasoning", ""),
        "route": route,
        "arm": arm,
        "conversation": conversation,
        "text": text,
        "warnings": warnings,
    }
