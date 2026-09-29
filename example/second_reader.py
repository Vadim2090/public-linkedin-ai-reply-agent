"""Independent, optional classification of the newest inbound message."""
import json
from pathlib import Path

from runtime import transport, typesafe

PROMPT = Path(__file__).resolve().parent / "prompts" / "second-reader-v1.json"
QUESTION_ID = "intent"
CONFIDENCE_FLOOR = 0.60


def question():
    """Read fresh on every call, matching the deploy behaviour of the other prompts."""
    return json.loads(PROMPT.read_text(encoding="utf-8"))


def build_state(messages):
    ordered = transport.ordered(messages)
    thread = [{"from": "them" if msg.inbound else "us", "text": msg.text.strip()}
              for msg in ordered]
    newest = next((msg["text"] for msg in reversed(thread) if msg["from"] == "them"), "")
    return {"thread": thread, "newest_inbound": newest}


def read(messages, pipeline_intent):
    try:
        payload = typesafe.ask(build_state(messages), {QUESTION_ID: question()})
        answer = payload["answers"][QUESTION_ID]
        choice = answer["choice"]
        confidence = float(answer["confidence"])
        probabilities = answer["probabilities"]
        disagrees = choice != pipeline_intent
        flagged = disagrees or choice == "unclear" or confidence < CONFIDENCE_FLOOR
        return {"choice": choice, "probabilities": probabilities,
                "confidence": confidence, "disagrees": disagrees, "flagged": flagged,
                "status": "ok", "input_tokens": payload.get("usage", {}).get("input_tokens")}
    except Exception:
        return {"choice": None, "probabilities": {}, "confidence": None,
                "disagrees": False, "flagged": True, "status": "unavailable",
                "input_tokens": None}
