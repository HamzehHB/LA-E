"""Deterministic synthetic synthesis collaborator for driver tests.

The formal LLM stage is mandatory, so tests that exercise the run driver
must supply a provider-like collaborator. This one returns validated
candidates through the real strict schema, so no network, no model, and
no local service is involved, and the tracked suite stays synthetic.
"""
import json

from src.living_authenticity.llm import LLMUnavailable, parse_llm_result

DEFAULT_TITLE = "Synthetic candidate"
DEFAULT_BODY = "Synthetic synthesis body for the driver test."
DEFAULT_TYPE = "Observation"


def synthetic_raw(title=DEFAULT_TITLE, body=DEFAULT_BODY,
                  suggested_type=DEFAULT_TYPE) -> str:
    """Return one well-formed model response body."""
    return json.dumps({"candidates": [{
        "title": title,
        "body": body,
        "suggested_type": suggested_type,
        "reason": "synthetic reason",
        "uncertainty": "synthetic uncertainty",
    }]})


class SyntheticSynthesis:
    """Return one valid candidate per unit without contacting anything."""

    name = "synthetic"

    def __init__(self, title=DEFAULT_TITLE, body=DEFAULT_BODY,
                 suggested_type=DEFAULT_TYPE, raw=None) -> None:
        self._raw = raw if raw is not None else synthetic_raw(
            title, body, suggested_type)
        self.calls = 0
        self.last_vault_context = None

    def synthesize(self, query=None, proposal=None, classification=None,
                   retrieval=None, comparisons=None, relation=None,
                   core=None, confidence=None, filter_outcome=None,
                   vault_context=None):
        self.calls += 1
        self.last_vault_context = vault_context
        result = parse_llm_result(self._raw)
        return (result, "synthetic prompt", "0" * 64)


class FailingSynthesis:
    """Always fail: proves the mandatory stage stops safely."""

    name = "disabled"

    def synthesize(self, *args, **kwargs):
        raise LLMUnavailable("llm_unavailable")


__all__ = ("FailingSynthesis", "SyntheticSynthesis", "synthetic_raw")
