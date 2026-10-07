"""Full-chain dataflow: identity passes stage to stage, LLM stops safe."""
import json

from src.living_authenticity.knowledge.orchestration.orchestrator import (
    EvidenceFirstPipeline,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)

def _unit():
    return KnowledgeUnit(id="q1", source="s",
                         original_text="alpha beta gamma delta epsilon",
                         meaning="alpha beta gamma delta epsilon")


def test_stage_identity_and_llm_safe_stop():
    from src.living_authenticity.knowledge.integration.unit_loop import (
        _handle_unit, _synthesize_candidates,
    )
    from src.living_authenticity.llm import (
        LLMUnavailable, Synthesis, build_llm_context,
    )

    pipe = EvidenceFirstPipeline()
    unit = pipe.run_unit(_unit())
    prompt = build_llm_context(
        unit.query_unit, unit.proposal, unit.classification,
        unit.retrieval_result, unit.comparisons, unit.relation_result,
        unit.core_analysis, unit.confidence, unit.filter_outcome)
    assert unit.query_unit.id in prompt or "SOURCE UNIT" in prompt

    class _Fake:
        name = "fake"
        seen = None

        def synthesize(self, query, proposal, classification, retrieval,
                       comparisons, relation, core, confidence, filt,
                       vault_context=None):
            assert query is unit.query_unit
            assert proposal is unit.proposal
            assert confidence is unit.confidence
            from src.living_authenticity.llm import parse_llm_result
            return (parse_llm_result(json.dumps(
                {"candidates": [{"title": "t", "body": "b",
                                 "suggested_type": "",
                                 "reason": "r",
                                 "uncertainty": "u"}]})),
                    "prompt", "digest")

    fake = _Fake()
    result, _, _ = _synthesize_candidates(fake, unit)
    assert result.candidates[0].body == "b"

    class _Failing:
        name = "disabled"

        def synthesize(self, *args, **kwargs):
            raise LLMUnavailable("llm_unavailable")

    entries: list = []
    audits: list = []
    _handle_unit("child.md", unit, synthesis=_Failing(), entries=entries,
                 audits=audits)
    assert entries[0].outcome == "llm_safe_stop"
    assert len(audits) == 1, (
        "safe stops persist a non-approved audit record, never approval")
    assert audits[0].approval_approved is False
    assert audits[0].execution_executed is False

