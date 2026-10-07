"""Mandatory driver-level synthesis behind the AI synthesis capability."""
import hashlib

from .base import LLMUnavailable
from .context import build_llm_context
from .registry import provider_limits
from .schema import LLMResult, parse_llm_result

class Synthesis:
    """Invoke the configured provider once per unit; never bypass."""

    def __init__(self, provider, confirm=None, limits=(3, 20000)) -> None:
        self._provider = provider
        self._confirm = confirm
        self._limits = limits

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def prompt_hash(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def synthesize(self, query=None, proposal=None, classification=None,
                   retrieval=None, comparisons=None, relation=None,
                   core=None, confidence=None,
                   filter_outcome=None, vault_context=None) -> tuple:
        """Return ``(LLMResult, prompt_text, prompt_sha256)`` or raise.

        ``vault_context`` is optional bounded excerpts (informational
        only): it is rendered into the prompt as context, never as
        authorization, and never changes proposal authority.
        """
        prompt = build_llm_context(
            query, proposal, classification, retrieval, comparisons,
            relation, core, confidence, filter_outcome,
            max_candidates=self._limits[0], vault_context=vault_context)
        provider = self._provider
        needs_gate = bool(
            getattr(provider, "requires_privacy_confirmation", False))
        if needs_gate:
            question = ("Non-local endpoint " + str(provider.endpoint)
                        + " would receive synthesis text. Proceed? [y/N]:")
            if self._confirm is not None:
                allowed = bool(self._confirm(question, prompt))
            else:
                print(question)
                print(prompt)
                try:
                    allowed = input().strip().lower() in ("y", "yes")
                except Exception:
                    allowed = False
            if not allowed:
                raise LLMUnavailable("llm_privacy_declined")
        raw = provider.complete(prompt)
        result = parse_llm_result(raw, self._limits[0], self._limits[1])
        return (result, prompt, self.prompt_hash(prompt))


def build_synthesis(provider, confirm=None, config=None) -> Synthesis:
    """Build the mandatory synthesis collaborator from config limits."""
    section = None
    if isinstance(config, dict):
        section = config.get("llm") if isinstance(
            config.get("llm"), dict) else config
    limits = provider_limits({"llm": section} if section else {})
    return Synthesis(provider, confirm=confirm, limits=limits)


__all__ = ("Synthesis", "build_synthesis")

