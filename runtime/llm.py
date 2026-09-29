"""Model calls over raw HTTP, with the provider/model/mode triple treated as one decision.

NO SDK, ON PURPOSE. A chat completion is one POST with a JSON body. Taking an SDK for it
buys a typed wrapper and costs a version to track, a breaking change to absorb, and a layer
between you and the wire exactly when a 400 needs reading. Swapping providers here is one
function with different headers.

THE LESSON THIS FILE IS SHAPED AROUND. Provider, model and "is this a reasoning model" are
ONE decision, and splitting them has cost two separate outages:

  * Mechanically: a reasoning model rejects `temperature` outright and wants
    `max_completion_tokens`; a non-reasoning model wants `temperature` and `max_tokens` and
    returns empty for the other shape. Setting the model without the mode gives you a 400
    on every call, or - worse - a silent empty string.
  * Commercially: the provider decides WHICH ACCOUNT PAYS. One stale `PROVIDER=openai` left
    in an environment pointed the highest-volume pass at an account that had been drained
    overnight, and the service returned "no credits remaining" on every call for nine hours.

So the model default FOLLOWS the provider, and the mode is inferred from the model, and an
explicit override still wins for a model this table has not met. No combination of
environment variables can produce a mismatched triple.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

# Optional measurement hook installed by evaluation tooling. It receives the pass name and
# provider usage object but cannot change the model response.
USAGE_HANDLER = None

# provider -> (default model, is_reasoning). The default model follows the provider so
# that switching provider returns the whole triple, not just the URL.
DEFAULTS = {
    "azure":  ("gpt-5-mini", True),
    "openai": ("gpt-4.1-mini", False),
    "stub":   ("stub", False),
}

_REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def resolve(prefix="LLM"):
    """(provider, model, is_reasoning) for a named pass, from the environment."""
    provider = os.environ.get(f"{prefix}_PROVIDER", "stub").lower()
    default_model, _ = DEFAULTS.get(provider, DEFAULTS["stub"])
    model = os.environ.get(f"{prefix}_MODEL", default_model)
    explicit = os.environ.get(f"{prefix}_IS_REASONING")
    if explicit is not None:
        reasoning = explicit.lower() == "true"
    else:
        # Inferred from the MODEL, not from the provider's default. That matters when only
        # half the pair is named: PROVIDER=openai MODEL=gpt-5 would otherwise inherit
        # `false`, send `temperature`, and 400 on every call.
        reasoning = model.startswith(_REASONING_PREFIXES)
    return provider, model, reasoning


def chat(messages, *, prefix="LLM", json_mode=False, max_tokens=1000,
         temperature=0.2, timeout=90, retries=4):
    """One chat completion. Returns the assistant's text.

    Retries on transport errors AND on 429/5xx, with a backoff. Not on a 400: a malformed
    request will be malformed the second time too, and retrying it turns a loud failure
    into a slow one.
    """
    provider, model, reasoning = resolve(prefix)

    if provider == "stub":
        return _stub(messages, json_mode)

    body = {"model": model, "messages": messages}
    if reasoning:
        body["max_completion_tokens"] = max_tokens        # NO temperature: it 400s
    else:
        body["max_tokens"] = max_tokens
        body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    if provider == "azure":
        endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
        # The versionless v1 surface: same dialect as OpenAI, deployment named in the body
        # as `model`. The older /openai/deployments/<name>/...?api-version=... route needs
        # the deployment in the URL and an api-version, and is the one that silently
        # breaks when a deployment is renamed.
        url = f"{endpoint}/openai/v1/chat/completions"
        headers = {"api-key": os.environ["AZURE_OPENAI_API_KEY"],
                   "Content-Type": "application/json"}
    else:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                   "Content-Type": "application/json"}

    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, data=json.dumps(body).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
            if USAGE_HANDLER is not None:
                USAGE_HANDLER(prefix, payload.get("usage", {}))
            return payload["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (408, 429, 500, 502, 503, 504, 529):
                raise
        except Exception as e:                       # transport: DNS, reset, timeout
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{prefix} call failed after {retries} attempts: {last}")


#: An optional `(messages, json_mode) -> str | None` installed by the CALLER.
#:
#: The runtime must not know what a support ticket is, but a demo or a test wants a stub
#: that answers sensibly for ITS domain. So the domain knowledge lives with the domain:
#: `example/` installs a handler here, and this module keeps knowing nothing. Returning
#: None falls through to the generic behaviour below.
STUB_HANDLER = None

#: `- label:  description` in a system prompt. How the generic stub discovers what it may
#: answer without this module knowing anything about the caller's domain. Weak on purpose:
#: it is a placeholder that keeps the pipeline runnable, not a classifier.
_LABEL_LINE = re.compile(r"^\s*[-*]\s+([a-z][a-z0-9_]{2,30})\s*:\s*(.+)$", re.M)
_WORD = re.compile(r"[a-z][a-z'-]{3,}")
# Words that appear in every description and so discriminate nothing.
_STOP = {"they", "that", "this", "with", "from", "them", "their", "want", "something",
         "needs", "reply", "message", "customer", "does", "what", "when", "have", "been"}


def _stub(messages, json_mode):
    """A deterministic offline provider, so the repository runs with no keys at all.

    This is not a mock in the test sense - it is a real code path, and the demo and the
    tests both take it. A reader who clones this repo should be able to see a card, press
    a button and watch the state machine move without signing up for anything.

    It stays DOMAIN-FREE by reading the labels out of the system prompt it was given
    (`- label: description` lines) and scoring each description's words against the
    conversation. It therefore knows nothing about support tickets, or about whatever
    domain replaces them, which is the same constraint the rest of `runtime/` lives under.
    """
    if STUB_HANDLER is not None:
        answered = STUB_HANDLER(messages, json_mode)
        if answered is not None:
            return answered

    system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
    user = "\n".join(m.get("content", "") for m in messages if m.get("role") != "system")

    if not json_mode:
        return ("Thanks for getting in touch. I have passed this to the team and you will "
                "hear back shortly.\n\n(stub provider: set LLM_PROVIDER to azure or openai "
                "for a real draft)")

    labels = _LABEL_LINE.findall(system)
    if not labels:
        return json.dumps({"intent": None, "confidence": "low", "flags": {},
                           "reasoning": "stub provider: no labels found in the system prompt"})

    haystack = set(_WORD.findall(user.lower())) - _STOP
    best, best_score = labels[0][0], -1
    for name, description in labels:
        words = (set(_WORD.findall(description.lower())) | {name.replace("_", " ")}) - _STOP
        score = sum(1 for w in words if w in haystack)
        # The label's own name appearing in the text is a strong signal on its own.
        score += 2 * sum(1 for part in name.split("_") if len(part) > 3 and part in haystack)
        if score > best_score:
            best, best_score = name, score

    return json.dumps({
        "intent": best,
        "confidence": "medium" if best_score > 0 else "low",
        "flags": {},
        "reasoning": f"stub provider: word overlap with the '{best}' description "
                     f"(score {best_score}), not a model",
    })
