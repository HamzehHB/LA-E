"""Formal LLM layer for the knowledge workflow.

The LLM layer is a first-class workflow component: it sits after the
Knowledge Filter and before human review, receives the real structured
outputs of the analytical stages, and produces candidate outputs for
review. Only the *provider* behind it is replaceable, selected through
``Config/llm.example.yaml`` / ``Config/llm.local.yaml``.

``LLM output != authorization``: suggested actions, confidence, and
reasoning from any provider are analysis material only. Governance
(action eligibility, approval, revalidation, execution, staging
destination) stays deterministic and unchanged.

Public surface:

* :class:`LLMProvider` / :class:`LLMUnavailable` — the provider contract.
* :func:`build_provider` — configuration-driven provider selection.
* ``locality`` — endpoint-based locality used by the privacy gate.
"""
from .base import LLMProvider, LLMUnavailable
from .context import build_llm_context, output_contract
from .normalize import normalize_model_text
from .orchestration import Synthesis, build_synthesis
from .registry import KNOWN_PROVIDERS, build_provider, provider_limits
from .schema import (
    CandidateOutput,
    LLMResult,
    candidate_response_schema,
    parse_llm_result,
)

__all__ = [
    "KNOWN_PROVIDERS",
    "CandidateOutput",
    "LLMProvider",
    "LLMResult",
    "LLMUnavailable",
    "Synthesis",
    "build_llm_context",
    "build_provider",
    "build_synthesis",
    "candidate_response_schema",
    "normalize_model_text",
    "output_contract",
    "parse_llm_result",
    "provider_limits",
]
