"""Knowledge retrieval tests.

Covers retrieval behavior (normal retrieval, empty input, no matches,
multiple matches, provenance preservation, determinism, malformed input,
Persian, English, mixed, Unicode/ZWNJ), result/candidate contracts,
corpus-store behavior, registry behavior, and the retrieval/decision
boundary. Security-specific tests live in test_retrieval_security.py.
"""

from dataclasses import FrozenInstanceError, asdict

import pytest

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.analysis.retrieval import (
    DefaultRetriever,
    InMemoryKnowledgeStore,
    KnowledgeRetriever,
    RetrievedCandidate,
    RetrievalResult,
    RetrieverRegistry,
    TokenOverlapRetriever,
    tokenize,
)


def _unit(
    body="",
    *,
    id="ku-1",
    source="s.md",
    position=0,
    cleaned_text=None,
    normalized_text=None,
    meaning=None,
    original_text=None,
):
    return KnowledgeUnit(
        id=id,
        source=source,
        original_text=body if original_text is None else original_text,
        cleaned_text=body if cleaned_text is None else cleaned_text,
        normalized_text=body if normalized_text is None else normalized_text,
        meaning=body if meaning is None else meaning,
        context="",
        position=position,
    )


def _retriever(**kwargs):
    return TokenOverlapRetriever(**kwargs)


class TestNormalRetrieval:
    def test_matching_unit_is_retrieved_with_provenance(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [
            _unit("calmness restores the mind slowly", id="c1", source="a.md", position=2),
            _unit("unrelated engine repair manual", id="c2", source="b.md"),
        ]
        result = _retriever().retrieve(query, corpus)
        assert result.query_unit_id == "q"
        assert result.query_source == "s.md"
        assert result.query_position == 0
        assert result.strategy == "token_overlap"
        assert result.corpus_size == 2
        assert len(result.candidates) == 1
        candidate = result.candidates[0]
        assert candidate.unit_id == "c1"
        assert candidate.source == "a.md"
        assert candidate.position == 2
        assert "calmness" in candidate.matched_terms
        assert candidate.overlap_score == len(candidate.matched_terms) >= 1
        assert candidate.retriever == "token_overlap"

    def test_query_own_id_is_excluded(self):
        query = _unit("calmness restores the mind", id="same")
        corpus = [_unit("calmness restores the mind", id="same", source="other.md")]
        result = _retriever().retrieve(query, corpus)
        assert result.candidates == ()

    def test_query_terms_are_preserved_for_later_stages(self):
        query = _unit("calmness restores the mind", id="q")
        result = _retriever().retrieve(query, [])
        assert "calmness" in result.query_terms
        assert result.query_terms == tuple(sorted(result.query_terms))

    def test_result_exposes_no_decision_fields(self):
        result = _retriever().retrieve(_unit("calmness restores mind", id="q"), [])
        payload = asdict(result)
        for forbidden in ("approved", "action", "duplicate", "update",
                           "merge", "novel", "relation", "core_conflict"):
            assert forbidden not in payload


class TestEmptyAndNoMatch:
    def test_empty_query_yields_empty_result_with_note(self):
        result = _retriever().retrieve(_unit("", id="q"), [_unit("calmness mind", id="c")])
        assert result.candidates == ()
        assert result.has_candidates is False
        assert result.query_terms == ()
        assert result.note != ""

    def test_whitespace_only_query_yields_empty_result(self):
        result = _retriever().retrieve(_unit("   \n  ", id="q"), [_unit("calmness mind", id="c")])
        assert result.candidates == ()
        assert "no analyzable content" in result.note.lower()

    def test_no_matches_yields_empty_result_with_note(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [_unit("engine repair manual torque", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.candidates == ()
        assert result.has_candidates is False
        assert result.corpus_size == 1
        assert result.note != ""

    def test_empty_corpus_yields_empty_result(self):
        result = _retriever().retrieve(_unit("calmness restores mind", id="q"), [])
        assert result.candidates == ()
        assert result.corpus_size == 0


class TestMultipleMatches:
    def test_ranked_by_overlap_descending(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [
            _unit("calmness alone here", id="weak", source="a.md"),
            _unit("calmness restores the mind fully today", id="strong", source="b.md"),
        ]
        result = _retriever().retrieve(query, corpus)
        assert [c.unit_id for c in result.candidates] == ["strong", "weak"]
        scores = [c.overlap_score for c in result.candidates]
        assert scores == sorted(scores, reverse=True)

    def test_max_candidates_limits_output(self):
        query = _unit("calmness restores the mind daily", id="q")
        corpus = [
            _unit("calmness restores the mind extra", id="c0", source="a.md", position=0),
            _unit("calmness restores the mind extra", id="c1", source="a.md", position=1),
            _unit("calmness restores the mind extra", id="c2", source="a.md", position=2),
        ]
        result = _retriever(min_shared_terms=1, max_candidates=2).retrieve(query, corpus)
        assert len(result.candidates) == 2

    def test_min_shared_terms_threshold(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [
            _unit("calmness elsewhere entirely", id="weak", source="a.md"),
            _unit("calmness restores the mind today", id="strong", source="b.md"),
        ]
        result = _retriever(min_shared_terms=3).retrieve(query, corpus)
        assert [c.unit_id for c in result.candidates] == ["strong"]

    def test_invalid_thresholds_rejected(self):
        with pytest.raises(ValueError):
            TokenOverlapRetriever(min_shared_terms=0)
        with pytest.raises(ValueError):
            TokenOverlapRetriever(max_candidates=-1)
        with pytest.raises(TypeError):
            TokenOverlapRetriever(min_shared_terms="2")  # type: ignore[arg-type]

    def test_max_candidates_zero_yields_empty_candidates(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [
            _unit("calmness restores the mind today", id="strong", source="b.md"),
        ]
        result = _retriever(min_shared_terms=1, max_candidates=0).retrieve(query, corpus)
        assert result.candidates == ()


class TestDeterminism:
    def test_repeated_runs_return_identical_results(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [
            _unit("the mind restores calmness", id="c1", source="b.md", position=1),
            _unit("calmness restores the mind", id="c2", source="a.md", position=1),
            _unit("mind restores calmness deeply now", id="c3", source="a.md", position=0),
        ]
        first = _retriever().retrieve(query, corpus)
        second = _retriever().retrieve(query, corpus)
        assert first == second

    def test_tie_break_is_stable_identity_order(self):
        query = _unit("calmness mind", id="q")
        corpus = [
            _unit("mind calmness", id="z", source="b.md", position=0),
            _unit("calmness mind", id="a", source="b.md", position=0),
            _unit("calmness mind", id="m", source="a.md", position=3),
        ]
        result = _retriever().retrieve(query, corpus)
        assert [c.unit_id for c in result.candidates] == ["m", "a", "z"]


class TestLanguages:
    def test_english_content(self):
        query = _unit("I observed people often lose calmness", id="q")
        corpus = [_unit("People often lose the ability to enjoy calmness", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.has_candidates is True

    def test_persian_content(self):
        query = _unit("متوجه شدم اغلب مردم آرامش را از دست می‌دهند", id="q")
        corpus = [_unit("اغلب مردم آرامش خود را از دست می‌دهند", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.has_candidates is True
        assert "مردم" in result.candidates[0].matched_terms

    def test_mixed_language_content(self):
        query = _unit("calmness یعنی آرامش ذهن", id="q")
        corpus = [_unit("آرامش ذهن restores calmness", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.has_candidates is True

    def test_zwnj_insensitive_matching(self):
        assert tokenize("می روم") == tokenize("می‌روم")
        query = _unit("می روم به خانه", id="q")
        corpus = [_unit("می‌روم به خانه", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.has_candidates is True

    def test_case_insensitive_matching(self):
        query = _unit("Calmness Restores Mind", id="q")
        corpus = [_unit("calmness restores mind today", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.has_candidates is True

class TestMalformedInput:
    def test_non_unit_query_rejected(self):
        with pytest.raises(TypeError):
            _retriever().retrieve("not a unit", [])  # type: ignore[arg-type]

    def test_non_unit_corpus_member_rejected(self):
        bad_corpus = ["nope"]  # type: ignore[list-item]
        with pytest.raises(TypeError):
            _retriever().retrieve(_unit("calmness mind", id="q"), bad_corpus)

    def test_unit_without_any_text_yields_empty_result(self):
        unit = KnowledgeUnit(id="q", source="s.md", original_text="", meaning="")
        result = _retriever().retrieve(unit, [_unit("calmness mind", id="c")])
        assert result.candidates == ()

    def test_fallback_text_fields_are_used(self):
        unit = KnowledgeUnit(
            id="q", source="s.md", original_text="",
            cleaned_text="", normalized_text="", meaning="calmness restores mind",
        )
        corpus = [_unit("calmness restores mind today", id="c")]
        result = _retriever().retrieve(unit, corpus)
        assert result.has_candidates is True


class TestImmutability:
    def test_query_unit_is_not_mutated(self):
        query = _unit("calmness restores the mind", id="q")
        before = asdict(query)
        _retriever().retrieve(query, [_unit("calmness restores mind", id="c")])
        assert asdict(query) == before

    def test_corpus_units_are_not_mutated(self):
        corpus = [_unit("calmness restores the mind", id="c", source="a.md")]
        before = [asdict(unit) for unit in corpus]
        _retriever().retrieve(_unit("calmness restores mind", id="q"), corpus)
        assert [asdict(unit) for unit in corpus] == before

    def test_result_and_candidate_are_frozen(self):
        result = _retriever().retrieve(_unit("calmness mind", id="q"), [])
        with pytest.raises(FrozenInstanceError):
            result.note = "changed"  # type: ignore[misc]
        candidate = RetrievedCandidate(unit_id="c", source="s", position=0, text="t")
        with pytest.raises(FrozenInstanceError):
            candidate.text = "changed"  # type: ignore[misc]

    def test_store_returns_defensive_copies(self):
        store = InMemoryKnowledgeStore([_unit("calmness mind", id="c")])
        listed = store.list_units()
        listed[0].position = 99
        assert store.list_units()[0].position == 0
        assert len(store) == 1


class TestStore:
    def test_add_and_list_preserves_order(self):
        store = InMemoryKnowledgeStore()
        store.add(_unit("calmness mind", id="c1"))
        store.add(_unit("calmness restores", id="c2"))
        assert [u.id for u in store.list_units()] == ["c1", "c2"]

    def test_rejects_non_units(self):
        store = InMemoryKnowledgeStore()
        with pytest.raises(TypeError):
            store.add("not a unit")  # type: ignore[arg-type]

    def test_retrieve_from_store_listing(self):
        store = InMemoryKnowledgeStore(
            [_unit("calmness restores the mind", id="c", source="kb.md")]
        )
        result = _retriever().retrieve(
            _unit("calmness restores mind", id="q"), store.list_units()
        )
        assert result.corpus_size == 1
        assert result.candidates[0].source == "kb.md"


class TestRegistry:
    def test_default_retriever_is_registered(self):
        registry = RetrieverRegistry()
        assert isinstance(registry.default(), TokenOverlapRetriever)

    def test_can_register_and_get_custom(self):
        registry = RetrieverRegistry()
        custom = DefaultRetriever()
        registry.register("custom", custom)
        assert registry.get("custom") is custom

    def test_duplicate_registration_rejected(self):
        registry = RetrieverRegistry()
        with pytest.raises(ValueError):
            registry.register("default", DefaultRetriever())

    def test_unknown_name_raises(self):
        registry = RetrieverRegistry()
        with pytest.raises(ValueError):
            registry.get("missing")


class TestRetrieverContract:
    def test_base_contract_is_abstract(self):
        with pytest.raises(TypeError):
            KnowledgeRetriever()  # type: ignore[abstract]

    def test_default_alias_matches_token_overlap(self):
        assert DefaultRetriever is TokenOverlapRetriever
        assert _retriever().name == "token_overlap"

    def test_retrieval_is_not_a_decision(self):
        query = _unit("calmness restores the mind", id="q")
        corpus = [_unit("calmness restores the mind fully", id="c")]
        result = _retriever().retrieve(query, corpus)
        assert result.has_candidates is True
        assert isinstance(result, RetrievalResult)
        assert not hasattr(result, "proposed_action")
        assert not hasattr(result, "confidence")
