"""Replaceable AI synthesis capability for the knowledge workflow.

``llm/`` is infrastructure, not a pipeline stage and not an
authority: it hosts the provider implementations (``disabled`` safe
default, ``ollama`` local, ``cloud`` explicit) plus the bounded
``Synthesis`` collaborator that the integration driver invokes once
per unit between the Knowledge Filter and human review. Only the
provider/model behind the capability is replaceable, selected through
``Config/llm.example.yaml`` / ``Config/llm.local.yaml``.

``LLM output != authorization``: suggested actions, confidence, and
reasoning from any provider are analysis material only. Governance
(action eligibility, approval, revalidation, execution, staging
destination) stays deterministic and unchanged. AI participates in
analysis and synthesis; it never approves, authorizes, executes, or
evolves knowledge.

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
