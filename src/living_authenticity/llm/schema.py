"""Strict result contract for the AI synthesis capability.

The contract is one JSON object with a ``candidates`` array. Every rule
here is enforced fail-closed: an unusable response stops the unit
instead of being repaired, guessed, or partially accepted.
"""
import json

from .base import LLMUnavailable
from .normalize import normalize_model_text

_ALLOWED_KEYS = ("title", "body", "suggested_type", "reason", "uncertainty")

# Structural guard against destination authority: the contract accepts
# exactly the five keys above, so a field naming a path, file, folder,
# destination, or vault location is structurally impossible. This is why
# the guard is an exact key allow-list rather than a substring scan over
# values: ordinary prose legitimately contains characters such as "/"
# (for example "input/output", dates, "and/or") and words containing
# "path" or "file" (for example "empathy", "profile"). Rejecting those
# would reject valid synthesis while adding no safety, because the write
# destination is never taken from LLM output — the executor derives the
# artifact name from the proposal hash and revalidates the exact
# displayed text before writing. An unknown key is rejected, not
# ignored, so no extra field can ever claim authority.


class CandidateOutput:
    """One validated synthesis candidate for human review."""

    def __init__(self, title="", body="", suggested_type="",
                 reason="", uncertainty="") -> None:
        self.title = title
        self.body = body
        self.suggested_type = suggested_type
        self.reason = reason
        self.uncertainty = uncertainty


class LLMResult:
    """Validated synthesis output: one tuple of candidates."""

    def __init__(self, candidates=()) -> None:
        self.candidates = tuple(candidates)


def candidate_response_schema(max_candidates: int = 3) -> dict:
    """Return the JSON schema for one synthesis response.

    This is the single machine-readable description of the contract. The
    prompt text is derived from the same rules, and a provider that can
    constrain decoding (for example a local server with structured
    output) is handed this schema so the model is grammar-bound to it.
    ``maxItems`` is open-ended on the lower side because zero
    candidates is a contract violation, not an empty success.
    """
    from ..knowledge.analysis.classification.result import CLASSIFICATION_TYPES

    return {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_candidates,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "suggested_type": {
                            "type": "string",
                            "enum": ["", *CLASSIFICATION_TYPES],
                        },
                        "reason": {"type": "string"},
                        "uncertainty": {"type": "string"},
                    },
                    "required": list(_ALLOWED_KEYS),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["candidates"],
        "additionalProperties": False,
    }


def _clean_entry(entry: dict, max_body_characters: int) -> CandidateOutput:
    from ..knowledge.analysis.classification.result import (
        CLASSIFICATION_TYPES,
    )
    if not isinstance(entry, dict):
        raise LLMUnavailable("llm_invalid_output: candidate is not an object")
    unknown = sorted(set(entry) - set(_ALLOWED_KEYS))
    if unknown:
        raise LLMUnavailable(
            "llm_invalid_output: unknown candidate fields: "
            + ", ".join(unknown))
    missing = [key for key in _ALLOWED_KEYS if key not in entry]
    if missing:
        raise LLMUnavailable(
            "llm_invalid_output: missing candidate fields: "
            + ", ".join(missing))
    title = entry["title"]
    body = entry["body"]
    reason = entry["reason"]
    uncertainty = entry["uncertainty"]
    suggested = entry["suggested_type"]
    for name, value in (("title", title), ("body", body),
                        ("suggested_type", suggested), ("reason", reason),
                        ("uncertainty", uncertainty)):
        if not isinstance(value, str):
            raise LLMUnavailable(
                "llm_invalid_output: field '" + name + "' must be a string")
    if not body.strip():
        raise LLMUnavailable("llm_invalid_output: candidate body is empty")
    if len(body) > max_body_characters:
        raise LLMUnavailable(
            "llm_invalid_output: candidate body exceeds the bound")
    if suggested not in CLASSIFICATION_TYPES:
        suggested = ""
    return CandidateOutput(title=title, body=body, suggested_type=suggested,
                           reason=reason, uncertainty=uncertainty)


def parse_llm_result(raw_text: str, max_candidates=3,
                     max_body_characters=20000) -> LLMResult:
    """Normalize, parse, and strictly validate one raw model response."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise LLMUnavailable("llm_invalid_output: empty response")
    text = normalize_model_text(raw_text)
    if not text:
        raise LLMUnavailable("llm_invalid_output: empty response")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise LLMUnavailable("llm_invalid_output: non-JSON") from exc
    if not isinstance(data, dict):
        raise LLMUnavailable("llm_invalid_output: unexpected shape")
    if set(data) - {"candidates"}:
        raise LLMUnavailable("llm_invalid_output: unknown top-level fields")
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise LLMUnavailable("llm_invalid_output: no candidates")
    cleaned = []
    for entry in candidates[:max_candidates]:
        cleaned.append(_clean_entry(entry, max_body_characters))
    if not cleaned:
        raise LLMUnavailable("llm_invalid_output: no usable candidate")
    return LLMResult(candidates=tuple(cleaned))


__all__ = ("CandidateOutput", "LLMResult", "candidate_response_schema",
           "parse_llm_result")
