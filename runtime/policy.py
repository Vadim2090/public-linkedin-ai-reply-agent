"""Bind a finding to a LINE OF THE PROMPT, not to a label a model typed.

THE BUG THIS EXISTS TO PREVENT, stated plainly, because it is not obvious and it cost a
week of a review loop's output.

When a reviewer corrects a draft, a correction pass names the rule the draft broke, so the
weekly pass can count how often each rule is broken and propose an edit to the ones that
recur. The obvious contract is "return the rule's name". That contract is unverifiable, and
in production it was wrong 12 times out of 42:

  * The policy document carried TWO independently numbered lists, both starting at 1. The
    same number named a different rule in each. "Rule 16" was ambiguous and neither the
    model nor the reader could tell which was meant.
  * Numbers the model produced were sometimes out of range entirely - a rule that does not
    exist. Nothing rejected it, because a string is a string.
  * An entire cluster of pricing corrections got filed under a rule about fabricating link
    content. The weekly pass then dutifully proposed editing that rule. The proposal was
    coherent, well argued, and aimed at the wrong paragraph.

None of that is visible downstream. The batch looks identical whether the binding held or
not, which is what makes it dangerous rather than merely wrong.

THE FIX. The correction pass returns the rule's OPENING LINE, copied character for
character from the policy it was given. The document then decides the label: we look the
quote up in the live prompt file and read the rule's identity off the file itself. A
paraphrase, a corrected typo or a renumbered prefix simply fails the lookup, and a failed
lookup is REPORTED rather than silently accepted.

So the model's job shrinks from "know which rule this is" to "copy this line", which is
the job models are reliable at. And the split between bound and unbound findings becomes a
number in the weekly header: the difference between "fixed" and "believed fixed".
"""
import re
from pathlib import Path

# A section heading in the policy file: an ALL-CAPS line, optionally with a parenthetical.
_SECTION = re.compile(r"^([A-Z][A-Z0-9 /&'-]{3,})\s*(?:\(([^)]*)\))?\s*:?\s*$")
# A numbered rule inside a section.
_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+")

# A quote shorter than this is not evidence: "NEVER." matches thirty places. Longer than
# this and the model has started summarising rather than copying.
MIN_QUOTE = 40
MAX_QUOTE = 400


def _norm(s):
    """Whitespace-insensitive comparison. A prompt file gets re-wrapped by editors and a
    model may join a wrapped line; neither should break the binding. Everything else -
    punctuation, case, wording - must match exactly, or it is not a copy."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def index(policy_text):
    """Map every line of the policy to (section, number) so a quote can be identified.

    Returns a list of dicts, one per non-blank line, in file order.
    """
    out, section = [], None
    for lineno, raw in enumerate(policy_text.splitlines(), 1):
        if not raw.strip():
            continue
        m = _SECTION.match(raw.strip())
        if m:
            section = m.group(1).strip()
            continue
        num = _NUMBERED.match(raw)
        out.append({
            "lineno": lineno,
            "section": section,
            "number": int(num.group(1)) if num else None,
            "text": raw.rstrip(),
            "norm": _norm(raw),
        })
    return out


def resolve(quote, policy_text):
    """Identify the rule a quote came from, or explain why it could not be identified.

    Returns (label, detail). `label` is None when the quote does not bind.

    Matching is deliberately strict and deliberately ordered:
      1. exact (whitespace-normalised) equality with a line
      2. a line that STARTS WITH the quote - the model copied the opening and stopped
      3. a line that CONTAINS the quote, only if that is unambiguous

    An ambiguous match is a FAILURE, not a coin flip. Two rules that share an opening
    clause are exactly the case where guessing puts a finding on the wrong paragraph, and
    that is the failure this module was written to end.
    """
    q = _norm(quote)
    if not q:
        return None, "no quote supplied"
    if len(q) < MIN_QUOTE:
        return None, f"quote is {len(q)} chars, under the {MIN_QUOTE} needed to identify a rule"
    if len(q) > MAX_QUOTE:
        return None, f"quote is {len(q)} chars, over {MAX_QUOTE}: summarised rather than copied"

    lines = index(policy_text)
    for predicate, why in (
        (lambda ln: ln["norm"] == q, "exact"),
        (lambda ln: ln["norm"].startswith(q), "prefix"),
        (lambda ln: q in ln["norm"], "substring"),
    ):
        hits = [ln for ln in lines if predicate(ln)]
        if len(hits) == 1:
            ln = hits[0]
            label = _label(ln)
            return label, f"{why} match, {label}, line {ln['lineno']}"
        if len(hits) > 1:
            return None, (f"quote matches {len(hits)} lines ({why}); ambiguous, so it is "
                          f"not bound rather than bound to a guess")
    return None, "quote not found in the policy: paraphrased, or the policy has changed"


def _label(line):
    section = line["section"] or "POLICY"
    return f"{section} {line['number']}" if line["number"] else section


def bind(violated_raw, quote, policy_path):
    """What the store records for one finding.

    `violated_raw` is what the model called the rule; it is kept, never trusted. The
    resolved label wins whenever the quote binds, and how often the document OVERRULED the
    model is itself reported - in production 12 of 42 labels were wrong, and a loop that
    does not measure that cannot know whether its clustering means anything.
    """
    text = Path(policy_path).read_text(encoding="utf-8") if Path(policy_path).exists() else ""
    label, detail = resolve(quote, text)
    return {
        "violated": label or (violated_raw or "").strip() or "unbound",
        "violated_raw": (violated_raw or "").strip(),
        "policy_quote": (quote or "").strip(),
        "tag_resolved": 1 if label else 0,
        "relabelled": bool(label and _norm(label) != _norm(violated_raw)),
        "detail": detail,
    }
