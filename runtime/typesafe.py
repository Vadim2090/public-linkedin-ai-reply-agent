"""Small, domain-free client for TypeSafe's System One endpoint.

The optional replay is keyed by the canonical request body, so callers can commit
responses for synthetic examples without teaching this module anything about the domain.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# Pinned, not `jev-latest`: an alias can move under thresholds that were tuned on a version.
DEFAULT_MODEL = "jev-1.13.0"
RETRYABLE = {408, 429, 500, 502, 503, 504, 529}

# Tests and callers may install a path without changing process-wide environment state.
REPLAY = None


def enabled():
    """Opt-in: a key or a replay file must be set, and SECOND_READER must not be `off`.

    A replay alone enables offline use, so the demo and the evaluation can be rerun from
    recorded responses without a key or a network.
    """
    return os.environ.get("SECOND_READER", "on").lower() != "off" and bool(
        os.environ.get("TYPESAFE_API_KEY") or _replay_path())


def request_body(state, questions, model=DEFAULT_MODEL):
    return {"state": state, "model": model, "questions": questions}


def request_key(body):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def ask(state, questions, *, model=DEFAULT_MODEL, timeout=30, retries=4):
    """Ask one System One request and return its decoded JSON response.

    Authentication and validation errors fail immediately. Transient HTTP and transport
    failures are retried. Errors are deliberately sanitised: neither the request headers
    nor the underlying exception (which can echo them) cross this boundary.
    """
    body = request_body(state, questions, model)
    replay = _read_replay()
    cached = replay.get(request_key(body))
    if cached is not None:
        return cached

    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("TypeSafe call unavailable: no API key or replay entry")

    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(ENDPOINT, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE:
                raise RuntimeError(f"TypeSafe call failed with HTTP {exc.code}") from None
        except Exception:
            pass
        if attempt + 1 < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"TypeSafe call failed after {retries} attempts")


def _replay_path():
    value = REPLAY or os.environ.get("TYPESAFE_REPLAY")
    return Path(value) if value else None


def _read_replay():
    path = _replay_path()
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("TypeSafe replay must be a JSON object")
    return data
