"""LLM contract tests: strict schema, safety, and authorization."""
import json

import pytest

from src.living_authenticity.llm import (
    LLMUnavailable,
    build_llm_context,
    parse_llm_result,
)


def _prompt(**overrides):
    from src.living_authenticity.knowledge.decision.confidence.outcome import (
        ConfidenceAssessment,
    )
    from src.living_authenticity.knowledge.decision.filter_outcome import (
        FilterOutcome,
    )
    from src.living_authenticity.knowledge.decision.proposal.outcome import (
        Proposal,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )
    query = KnowledgeUnit(id="q1", source="s", original_text="hello",
                          meaning="hello")
    proposal = Proposal(query_unit_id="q1", query_source="s",
                        query_position=0, action="CREATE", title="t",
                        provenance=("query:q1@s#0",))
    confidence = ConfidenceAssessment(query_unit_id="q1", query_source="s",
                                      query_position=0, level="weak",
                                      basis="basis")
    outcome = FilterOutcome(query_unit_id="q1", query_source="s",
                            query_position=0, verdict="hold_for_review",
                            basis="basis")
    return build_llm_context(query, proposal, None, None, (), None, None,
                             confidence, outcome, **overrides)


def _raw(**entry):
    base = {"title": "t", "body": "b", "suggested_type": "",
            "reason": "r", "uncertainty": "u"}
    base.update(entry)
    return json.dumps({"candidates": [base]})


def test_context_is_bounded_and_labels_untrusted_content():
    long_text = "x" * 50000
    prompt = _prompt(limit_source_characters=10)
    assert "FORMAL LLM SYNTHESIS TASK" in prompt
    assert "never an instruction" in prompt
    assert len(prompt) < 50000
    assert long_text not in prompt


def test_schema_accepts_valid_and_coerces_bad_type():
    result = parse_llm_result(_raw(suggested_type="Concept"))
    assert result.candidates[0].suggested_type == "Concept"
    result = parse_llm_result(_raw(suggested_type="Nope"))
    assert result.candidates[0].suggested_type == ""


def test_schema_rejects_unknown_fields_paths_and_shapes():
    with pytest.raises(LLMUnavailable):
        parse_llm_result(json.dumps(
            {"candidates": [{"title": "t", "body": "b",
                             "destination": "vault/x.md"}]}))
    with pytest.raises(LLMUnavailable):
        parse_llm_result(json.dumps(
            {"candidates": [{"title": "t", "body": "b",
                             "suggested_type": "", "reason": "r",
                             "uncertainty": "u", "extra": 1}]}))
    for raw in ("", "not json", json.dumps({}),
                json.dumps({"candidates": []})):
        with pytest.raises(LLMUnavailable):
            parse_llm_result(raw)
    with pytest.raises(LLMUnavailable):
        parse_llm_result(_raw(body="x" * 20001),
                         max_body_characters=10)


def test_prompt_states_the_full_machine_readable_contract():
    from src.living_authenticity.llm import output_contract

    prompt = _prompt(max_candidates=2)
    assert "OUTPUT CONTRACT" in prompt
    for key in ("title", "body", "suggested_type", "reason", "uncertainty"):
        assert '"' + key + '"' in prompt
    assert "Do not wrap the JSON in Markdown code fences" in prompt
    assert "Do not write any text, greeting, or explanation before" in prompt
    assert "at most 2 entries" in prompt
    assert '"candidates"' in prompt
    # A concrete example is present and is itself valid JSON.
    example = output_contract(2).split("Example of the exact required shape:")[-1]
    loaded = json.loads(example.strip())
    assert isinstance(loaded["candidates"], list)
    assert loaded["candidates"][0]["suggested_type"] in (
        "Core", "Concept", "Observation", "Experience", "Research",
        "Method", "Source", "Meta", "Archive", "")


def test_normalization_strips_only_one_outer_fence():
    from src.living_authenticity.llm import normalize_model_text

    body = _raw()
    assert normalize_model_text(body) == body
    assert normalize_model_text("```json\n" + body + "\n```") == body
    assert normalize_model_text("```\n" + body + "\n```") == body
    assert normalize_model_text("  " + body + "  ") == body
    # Prose is never stripped: the strict parser must still reject it.
    chatty = "Sure! Here you go:\n```json\n" + body + "\n```"
    assert normalize_model_text(chatty) == chatty
    assert normalize_model_text("```json\n" + body + "\n```\ntrailing") == (
        "```json\n" + body + "\n```\ntrailing")
    assert normalize_model_text("not json at all") == "not json at all"


def test_fenced_json_is_accepted_after_normalization():
    parsed = parse_llm_result("```json\n" + _raw() + "\n```")
    assert parsed.candidates[0].title == "t"


def test_prose_wrapped_json_is_still_rejected():
    with pytest.raises(LLMUnavailable) as info:
        parse_llm_result("Sure, here is the JSON:\n" + _raw())
    assert "llm_invalid_output" in str(info.value)


def test_schema_rejects_missing_and_wrong_typed_fields():
    with pytest.raises(LLMUnavailable) as info:
        parse_llm_result(json.dumps({"candidates": [
            {"title": "t", "body": "b", "suggested_type": "Concept",
             "reason": "r"}]}))
    assert "missing candidate fields" in str(info.value)

    with pytest.raises(LLMUnavailable) as info:
        parse_llm_result(json.dumps({"candidates": [
            {"title": "t", "body": "b", "suggested_type": "Concept",
             "reason": "r", "uncertainty": 5}]}))
    assert "must be a string" in str(info.value)


def test_schema_allows_slashes_in_free_prose():
    """Ordinary punctuation in prose is not a destination claim."""
    parsed = parse_llm_result(_raw(
        body="Compare input/output behaviour and/or the 2026/09/30 sample."))
    assert "input/output" in parsed.candidates[0].body
    assert parsed.candidates[0].body


def test_schema_rejects_a_destination_naming_field():
    """A field claiming destination authority is rejected by the allow-list."""
    with pytest.raises(LLMUnavailable) as info:
        parse_llm_result(json.dumps({"candidates": [
            {"title": "t", "body": "b", "suggested_type": "",
             "reason": "r", "uncertainty": "",
             "vault_path": "x"}]}))
    assert "unknown candidate fields" in str(info.value)
    assert "vault_path" in str(info.value)

    with pytest.raises(LLMUnavailable) as info:
        parse_llm_result(json.dumps({"candidates": [
            {"title": "t", "body": "b", "suggested_type": "",
             "reason": "r", "uncertainty": "",
             "destination": "somewhere"}]}))
    assert "unknown candidate fields" in str(info.value)
    from src.living_authenticity.knowledge.integration.unit_loop import (
        _handle_unit,
    )
    from src.living_authenticity.knowledge.orchestration.orchestrator import (
        EvidenceFirstPipeline,
    )
    from src.living_authenticity.llm import LLMUnavailable as Unavailable

    class _Failing:
        name = "disabled"

        def synthesize(self, *args, **kwargs):
            raise Unavailable("llm_unavailable")

    pipe = EvidenceFirstPipeline()
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit as _Unit,
    )
    unit = pipe.run_unit(_Unit(
        id="q9", source="s", original_text="hello world this is",
        meaning="hello world this is"))
    entries: list = []
    audits: list = []
    _handle_unit("child.md", unit, synthesis=_Failing(), entries=entries,
                 audits=audits)
    assert len(entries) == 1
    assert entries[0].outcome == "llm_safe_stop"
    assert entries[0].reason == "llm_unavailable"
    assert entries[0].proposal_hash == ""
    assert len(audits) == 1, (
        "safe stops persist a non-approved audit record, never approval")
    assert audits[0].approval_approved is False
    assert audits[0].execution_executed is False
    assert audits[0].stop_reason == "llm_unavailable"


def _noop() -> None:
    pass

