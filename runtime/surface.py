"""The review surface: where a human sees a draft and decides.

CHAT IS THE ENTIRE INTERFACE. No web app was built for this, and that is the single
largest scope decision in the system it came from: zero UI code, zero auth to build, zero
hosting, and the reviewer is already in the chat client all day. A separate dashboard would
be a second place to visit, and any review workflow with a second place to visit decays.

Two things in here are load-bearing and easy to get wrong.

THE THREE-SECOND PROBLEM. Chat platforms kill an interaction that is not acknowledged
within about three seconds, but sending a message takes longer than that. So: verify the
request, hand the work to a thread, return 200 immediately, and edit the card when the work
finishes. Doing the send inline gives the reviewer a timeout error on an action that
actually succeeded, and then they click again.

THREE-TIER CARD FALLBACK. A full card can be rejected for exceeding the platform's block
limits - a long conversation does it. When that happened, no card appeared, nothing logged
an error, and a waiting customer was simply dropped on the floor. So: full card, else a
minimal card with just the draft and the buttons, else a plain-text alert. A degraded card
is recoverable; a missing one is invisible.
"""
import hashlib
import hmac
import json
import logging
import os
import threading
import time

log = logging.getLogger("runtime.surface")

MAX_SECTION = 2900          # platform per-block text cap, with headroom
REPLAY_WINDOW = 60 * 5      # a signed request older than this is refused


# ------------------------------------------------------------------ request authenticity

def verify_signature(raw_body: bytes, timestamp: str, signature: str, secret: str = None) -> bool:
    """Is this request really from the chat platform?

    FAILS CLOSED WHEN UNCONFIGURED. A missing secret returns False rather than True. The
    permissive version of this line means anyone who learns the URL can approve messages
    that go out under your name, and it looks identical in every log until they do.

    The replay window matters as much as the signature: a valid signed request captured
    once is valid forever without it.

    `compare_digest`, not `==`. String comparison short-circuits on the first differing
    byte, which leaks the prefix length through timing.
    """
    secret = secret if secret is not None else os.environ.get("SIGNING_SECRET", "")
    if not secret:
        log.error("no signing secret configured - refusing every request rather than "
                  "accepting every request")
        return False
    try:
        if abs(time.time() - int(timestamp)) > REPLAY_WINDOW:
            return False
    except (TypeError, ValueError):
        return False
    base = b"v0:" + str(timestamp).encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def handle_async(fn, *args, **kwargs):
    """Answer the platform now, do the work after.

    The caller returns 200 the moment this returns. Anything that can take longer than a
    second - a model call, an outbound send, a card edit - belongs in here.
    """
    t = threading.Thread(target=_guarded, args=(fn,) + args, kwargs=kwargs, daemon=True)
    t.start()
    return t


def _guarded(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except Exception:
        # A thread that dies silently is a card that never updates and a reviewer who
        # clicks again.
        log.exception("background handler failed")


# ---------------------------------------------------------------------------- the card

def _trim(text, cap=MAX_SECTION, keep="head"):
    text = text or ""
    if len(text) <= cap:
        return text
    return (text[:cap - 20] + "\n... [trimmed]") if keep == "head" else ("... [trimmed]\n" + text[-(cap - 20):])


def build_card(draft: dict, request_id: str, version: int = 1, allow_fix: bool = True):
    """The full card. Everything a reviewer needs to decide WITHOUT opening another tab.

    The classifier's own reasoning is on the card on purpose: it is what makes the system
    debuggable rather than magic. A reviewer who can see WHY a route was chosen can tell a
    wording problem from a routing problem, and those get fixed in different places.

    Button values carry `request_id:version`, never a bare id. A button is a frozen
    snapshot: once v2 exists, the v1 card is still sitting in the channel with working
    buttons, and approving there would send superseded text. The version travels with the
    click so the handler can refuse a stale one.
    """
    value = f"{request_id}:{version}"
    conf = draft.get("confidence")
    warn = "  :warning: LOW CONFIDENCE" if conf == "low" else ""
    header = f"*New draft* - *{draft.get('intent', '?')}*" + (f" ({conf})" if conf else "") + warn

    meta = "\n".join(filter(None, [
        f"*Subject:* {draft.get('subject_name') or draft.get('subject_id') or '(unknown)'}",
        f"*Flags on:* {', '.join(k for k, v in (draft.get('flags') or {}).items() if v) or 'none'}",
        f"*Why:* {draft.get('reasoning', '')}",
    ]))

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _trim(meta)}},
    ]
    convo = _trim(draft.get("conversation", ""), 2400, keep="tail")
    if convo.strip():
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": f"*Conversation:*\n```{convo}```"}})
    body = _trim(draft.get("text", ""), 2600)
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
                   "text": "*Draft:*\n>" + body.replace("\n", "\n>")}})

    elements = [{"type": "button", "action_id": "approve", "style": "primary",
                 "text": {"type": "plain_text", "text": "Approve & send"}, "value": value}]
    if allow_fix:
        elements.append({"type": "button", "action_id": "fix",
                         "text": {"type": "plain_text", "text": "Fix"}, "value": value})
    elements.append({"type": "button", "action_id": "decline", "style": "danger",
                     "text": {"type": "plain_text", "text": "Decline"}, "value": value})
    blocks.append({"type": "actions", "block_id": "approval", "elements": elements})
    return blocks


def build_minimal_card(draft: dict, request_id: str, version: int = 1):
    """Tier two: the draft and the buttons, nothing else. Guaranteed small.

    Built after a real incident: one long conversation produced a card over the platform's
    size limit, the post was rejected, no card appeared, and nobody knew a waiting customer
    had been dropped.
    """
    value = f"{request_id}:{version}"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"*New draft* - *{draft.get('intent', '?')}*\n"
            ":warning: _full card exceeded the size limit; showing the draft only._"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": "*Draft:*\n>" + _trim(draft.get("text", ""), 2000).replace("\n", "\n>")}},
        {"type": "actions", "block_id": "approval", "elements": [
            {"type": "button", "action_id": "approve", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve & send"}, "value": value},
            {"type": "button", "action_id": "decline", "style": "danger",
             "text": {"type": "plain_text", "text": "Decline"}, "value": value}]},
    ]


def post_card(poster, draft, request_id, version=1, allow_fix=True):
    """Post with the three-tier fallback. Returns (ok, channel, ts).

    `poster(blocks, text) -> (ok, channel, ts)` is injected so this module has no HTTP
    client and no platform dependency. The console surface below is a poster; so is a real
    chat client.

    A total failure is logged at ERROR and returns False. It must never pass silently:
    "nothing needed a decision" and "the decision never reached anyone" have to look
    different.
    """
    for build, label in ((lambda: build_card(draft, request_id, version, allow_fix), "full"),
                         (lambda: build_minimal_card(draft, request_id, version), "minimal")):
        try:
            ok, channel, ts = poster(build(), f"New draft for {draft.get('subject_id', '?')}")
            if ok:
                return True, channel, ts
            log.warning("%s card rejected for %s, falling back", label, request_id[:8])
        except Exception:
            log.exception("%s card raised for %s, falling back", label, request_id[:8])
    try:
        ok, channel, ts = poster(None, f"Draft {request_id[:8]} needs review (card could not render)")
        if ok:
            return True, channel, ts
    except Exception:
        log.exception("plain-text fallback raised for %s", request_id[:8])
    log.error("CARD POST FAILED for %s - nobody has been asked. Needs manual handling.",
              request_id[:8])
    return False, None, None


# ------------------------------------------------------------------- a console surface

class ConsolePoster:
    """Prints the card to stdout and records it, so the repository is runnable with no
    chat workspace, no tunnel and no tokens. Same contract as a real poster."""

    def __init__(self):
        self.posted = []

    def __call__(self, blocks, fallback_text):
        self.posted.append({"blocks": blocks, "text": fallback_text})
        if blocks is None:
            print(f"\n[plain text] {fallback_text}")
            return True, "console", str(len(self.posted))
        print("\n" + "=" * 72)
        for b in blocks:
            if b["type"] == "section":
                print(_plain(b["text"]["text"]))
            elif b["type"] == "actions":
                print("  [" + "]  [".join(e["text"]["text"] for e in b["elements"]) + "]")
        print("=" * 72)
        return True, "console", str(len(self.posted))


def _plain(markdown_text):
    out = markdown_text.replace("*", "").replace("```", "")
    return "\n".join("  " + line for line in out.splitlines())


def parse_button_value(value: str):
    """`request_id:version` -> (request_id, version). A value without a version is from a
    card posted before versions existed; treat it as v1 rather than rejecting it."""
    if ":" not in (value or ""):
        return value, 1
    rid, _, ver = value.rpartition(":")
    try:
        return rid, int(ver)
    except ValueError:
        return value, 1
