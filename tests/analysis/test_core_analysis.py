"""Analytical Core relevance analysis tests."""

from dataclasses import FrozenInstanceError, asdict

import pytest

from src.living_authenticity.knowledge.analysis.core_analysis import (
    CORE_RELEVANCE_VALUES,
    AnalyzerRegistry,
    CoreAnalysisResult,
    CoreRelevanceAnalyzer,
    TokenOverlapCoreAnalyzer,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit


from tests._test_core_helpers import build_test_validated_core


def _unit(body="", *, id="q", source="s.md", position=0):
    return KnowledgeUnit(
        id=id, source=source, original_text=body, cleaned_text=body,
        normalized_text=body, meaning=body, context="", position=position,
    )


def _core(body, *, id="core1", source="core.md", position=1):
    u = _unit(body, id=id, source=source, position=position)
    u.is_core = True
    u.core_hash = "mock_core_hash_test_core_analysis"
    u.core_source_path = source
    return u


def _analyze(query, cores):
    cores_list = list(cores) if cores is not None else []
    val_core = build_test_validated_core(cores_list, source_path="core.md", core_hash="mock_core_hash_test_core_analysis")
    return TokenOverlapCoreAnalyzer().analyze(query, cores_list, context=val_core)


class TestRelevance:
    def test_potentially_relevant(self):
        query = _unit("calmness restores the mind and body balance")
        core = _core("calmness restores the mind during evening practice")
        result = _analyze(query, [core])
        assert result.relevance == "potentially_relevant"
        assert result.is_potentially_relevant is True
        assert result.shared_terms == ("calmness", "mind", "restores", "the")
        assert result.examined_core_ids == ("core1",)

    def test_no_observed_relevance(self):
        query = _unit("calmness restores the mind")
        core = _core("engine repair manual tractor")
        result = _analyze(query, [core])
        assert result.relevance == "no_observed_relevance"
        assert result.is_potentially_relevant is False

    def test_single_shared_term_is_not_enough(self):
        query = _unit("calmness restores the mind")
        core = _core("calmness engine tractor repair")
        result = _analyze(query, [core])
        assert result.relevance == "no_observed_relevance"

    def test_empty_query_is_insufficient(self):
        result = _analyze(_unit(""), [_core("calmness mind")])
        assert result.relevance == "insufficient_evidence"

    def test_no_core_supplied_raises_error(self):
        with pytest.raises(ValueError, match="core_units must contain at least one validated KnowledgeUnit"):
            _analyze(_unit("calmness mind"), [])

    def test_closed_vocabulary(self):
        assert set(CORE_RELEVANCE_VALUES) == {
            "potentially_relevant", "no_observed_relevance", "insufficient_evidence",
        }


class TestContract:
    def test_provenance(self):
        query = _unit("calmness mind", id="q1", source="q.md", position=3)
        result = _analyze(query, [_core("calmness mind")])
        assert result.query_unit_id == "q1"
        assert result.query_source == "q.md"
        assert result.query_position == 3
        assert result.strategy == "token_overlap_core_analysis"

    def test_deterministic(self):
        query = _unit("calmness restores the mind")
        cores = [_core("calmness restores the mind slowly"), _core("engine repair")]
        first = _analyze(query, cores)
        second = _analyze(query, cores)
        assert first == second

    def test_frozen(self):
        result = _analyze(
            _unit("calmness mind"), [_core("calmness mind")])
        with pytest.raises(FrozenInstanceError):
            result.note = "x"  # type: ignore[misc]

    def test_non_authoritative(self):
        result = _analyze(
            _unit("calmness restores the mind"),
            [_core("calmness restores the mind slowly")])
        assert result.is_authoritative is False
        assert result.requires_human_review is True
        payload = asdict(result)
        for field in ("approved", "execute", "action", "confidence",
                      "target_path", "delete", "update", "merge", "archive"):
            assert field not in payload

    def test_no_mutation(self):
        query = _unit("calmness mind", id="q")
        core = _core("calmness mind fully", id="c1")
        before_q, before_c = asdict(query), asdict(core)
        _analyze(query, [core])
        assert asdict(query) == before_q
        assert asdict(core) == before_c

    def test_multilingual(self):
        body = "Aramesh baraye zohn"
        result = _analyze(_unit(body), [_core(body)])
        assert result.relevance == "potentially_relevant"


class TestInvalid:
    def test_bad_query(self):
        with pytest.raises(TypeError):
            TokenOverlapCoreAnalyzer().analyze("nope", [])  # type: ignore[arg-type]

    def test_bad_core_item(self):
        with pytest.raises(TypeError):
            TokenOverlapCoreAnalyzer().analyze(_unit("t"), ["no"])  # type: ignore[list-item]

    def test_none_cores(self):
        with pytest.raises(TypeError):
            TokenOverlapCoreAnalyzer().analyze(_unit("t"), None)  # type: ignore[arg-type]


class TestRegistry:
    def test_abstract(self):
        with pytest.raises(TypeError):
            CoreRelevanceAnalyzer()  # type: ignore[abstract]

    def test_registry(self):
        reg = AnalyzerRegistry()
        assert isinstance(reg.default(), TokenOverlapCoreAnalyzer)
        custom = TokenOverlapCoreAnalyzer()
        reg.register("custom", custom)
        assert reg.get("custom") is custom
        with pytest.raises(ValueError):
            reg.register("default", TokenOverlapCoreAnalyzer())
        with pytest.raises(ValueError):
            reg.get("missing")

    def test_result_type(self):
        result = _analyze(
            _unit("calmness mind"), [_core("calmness mind")])
        assert isinstance(result, CoreAnalysisResult)
