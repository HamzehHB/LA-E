"""Unit tests for the five distinct retrieval capabilities.

Validates:
1. Capability 1: Query Preparation (prepare_query)
2. Capability 2: Candidate Retrieval (retrieve_candidates)
3. Capability 3: Relevance Assessment (assess_relevance)
4. Capability 4: Candidate Identification (identify_candidates)
5. Capability 5: Retrieval Evidence Recording (record_evidence)
6. RetrievalCoordinator sequential orchestration and observational events
"""

import pytest

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.analysis.retrieval.pipeline import (
    PreparedQuery,
    RelevanceAssessment,
    RetrievalCoordinator,
    assess_relevance,
    identify_candidates,
    prepare_query,
    record_evidence,
    retrieve_candidates,
)
from src.living_authenticity.knowledge.analysis.retrieval.token_overlap import TokenOverlapRetriever


def _make_unit(
    body: str,
    *,
    id: str = "ku-test",
    source: str = "source.md",
    position: int = 0,
) -> KnowledgeUnit:
    return KnowledgeUnit(
        id=id,
        source=source,
        original_text=body,
        cleaned_text=body,
        normalized_text=body,
        meaning=body,
        context="",
        position=position,
    )


class TestCapability1QueryPreparation:
    def test_prepare_query_extracts_tokens_and_metadata(self):
        unit = _make_unit("Authentic presence creates clarity and awareness.", id="q-1", source="notes.md", position=3)
        prepared = prepare_query(unit)

        assert isinstance(prepared, PreparedQuery)
        assert prepared.query_unit_id == "q-1"
        assert prepared.query_source == "notes.md"
        assert prepared.query_position == 3
        assert "presence" in prepared.query_terms
        assert "clarity" in prepared.query_terms
        assert prepared.query_terms == tuple(sorted(prepared.query_terms))
        assert prepared.context_metadata["term_count"] == len(prepared.query_terms)

    def test_prepare_query_type_safety(self):
        with pytest.raises(TypeError):
            prepare_query("not a knowledge unit")  # type: ignore[arg-type]


class TestCapability2CandidateRetrieval:
    def test_retrieve_candidates_finds_matching_units(self):
        query = prepare_query(_make_unit("presence and calmness", id="q-2"))
        corpus = [
            _make_unit("presence of mind in action", id="c-1", source="c1.md", position=0),
            _make_unit("complete silence and calmness", id="c-2", source="c2.md", position=1),
            _make_unit("totally unrelated hardware repair", id="c-3", source="c3.md", position=2),
        ]
        matches = retrieve_candidates(query, corpus, min_shared_terms=1)
        assert len(matches) == 2
        match_ids = {m.id for m in matches}
        assert match_ids == {"c-1", "c-2"}

    def test_retrieve_candidates_never_retrieves_self(self):
        query = prepare_query(_make_unit("presence and calmness", id="q-self"))
        corpus = [
            _make_unit("presence and calmness", id="q-self", source="other.md"),
            _make_unit("presence and calmness", id="other-id", source="other.md"),
        ]
        matches = retrieve_candidates(query, corpus)
        assert len(matches) == 1
        assert matches[0].id == "other-id"

    def test_retrieve_candidates_respects_min_shared_terms(self):
        query = prepare_query(_make_unit("presence awareness calmness", id="q-3"))
        corpus = [
            _make_unit("presence alone", id="weak"),
            _make_unit("presence awareness and calmness combined", id="strong"),
        ]
        matches = retrieve_candidates(query, corpus, min_shared_terms=2)
        assert len(matches) == 1
        assert matches[0].id == "strong"

    def test_retrieve_candidates_type_safety(self):
        query = prepare_query(_make_unit("presence", id="q-4"))
        with pytest.raises(TypeError):
            retrieve_candidates(query, ["not a unit"])  # type: ignore[list-item]


class TestCapability3RelevanceAssessment:
    def test_assess_relevance_produces_explicit_textual_evidence(self):
        query = prepare_query(_make_unit("presence and calmness", id="q-5"))
        candidates = [
            _make_unit("presence of mind", id="c-1", source="s1.md", position=1),
        ]
        assessments = assess_relevance(query, candidates, strategy="token_overlap")
        assert len(assessments) == 1
        ass = assessments[0]
        assert isinstance(ass, RelevanceAssessment)
        assert ass.candidate_unit_id == "c-1"
        assert ass.overlap_score == 1
        assert ass.matched_terms == ("presence",)
        assert "presence" in ass.evidence_basis
        assert "Shares 1 content token(s)" in ass.evidence_basis


class TestCapability4CandidateIdentification:
    def test_identify_candidates_preserves_stable_identity_and_ranking(self):
        query = prepare_query(_make_unit("presence awareness calmness", id="q-6"))
        c1 = _make_unit("presence alone", id="c1", source="b.md", position=1)
        c2 = _make_unit("presence awareness calmness deeply", id="c2", source="a.md", position=0)
        c3 = _make_unit("presence awareness", id="c3", source="a.md", position=2)

        assessments = assess_relevance(query, [c1, c2, c3])
        identified = identify_candidates(assessments, max_candidates=2)

        assert len(identified) == 2
        assert identified[0].unit_id == "c2"
        assert identified[1].unit_id == "c3"
        assert identified[0].candidate_identity_key == "a.md:0:c2"
        assert identified[1].candidate_identity_key == "a.md:2:c3"
        assert identified[0].retriever == "token_overlap"

    def test_identify_candidates_zero_or_negative_returns_empty(self):
        a1 = RelevanceAssessment(
            candidate_unit_id="c-1",
            candidate_source="s.md",
            candidate_position=0,
            candidate_text="text",
            overlap_score=2,
            matched_terms=("t",),
            similarity_score=2.0,
            evidence_basis="match",
            strategy="token_overlap",
        )
        assert identify_candidates([a1], max_candidates=0) == ()
        assert identify_candidates([a1], max_candidates=-1) == ()
        with pytest.raises(TypeError):
            identify_candidates([a1], max_candidates="5")  # type: ignore[arg-type]

    def test_identify_candidates_deduplication_preserves_top_assessment(self):
        a1 = RelevanceAssessment(
            candidate_unit_id="c-dup",
            candidate_source="source.md",
            candidate_position=0,
            candidate_text="presence text",
            overlap_score=3,
            matched_terms=("calmness", "clarity", "presence"),
            similarity_score=3.0,
            evidence_basis="high match",
            strategy="token_overlap",
        )
        a2 = RelevanceAssessment(
            candidate_unit_id="c-dup",
            candidate_source="source.md",
            candidate_position=0,
            candidate_text="presence text",
            overlap_score=1,
            matched_terms=("presence",),
            similarity_score=1.0,
            evidence_basis="lower match",
            strategy="token_overlap",
        )
        identified = identify_candidates([a2, a1], max_candidates=5)
        assert len(identified) == 1
        assert identified[0].unit_id == "c-dup"
        assert identified[0].overlap_score == 3
        assert identified[0].candidate_identity_key == "source.md:0:c-dup"

    def test_identify_candidates_preserves_distinct_items_with_differing_source_or_position(self):
        a1 = RelevanceAssessment(
            candidate_unit_id="c-same",
            candidate_source="source1.md",
            candidate_position=0,
            candidate_text="doc 1",
            overlap_score=2,
            matched_terms=("a", "b"),
            similarity_score=2.0,
            evidence_basis="s1",
            strategy="token_overlap",
        )
        a2 = RelevanceAssessment(
            candidate_unit_id="c-same",
            candidate_source="source2.md",
            candidate_position=0,
            candidate_text="doc 2",
            overlap_score=2,
            matched_terms=("a", "b"),
            similarity_score=2.0,
            evidence_basis="s2",
            strategy="token_overlap",
        )
        a3 = RelevanceAssessment(
            candidate_unit_id="c-same",
            candidate_source="source1.md",
            candidate_position=5,
            candidate_text="doc 1 later",
            overlap_score=2,
            matched_terms=("a", "b"),
            similarity_score=2.0,
            evidence_basis="s1 pos 5",
            strategy="token_overlap",
        )
        identified = identify_candidates([a1, a2, a3], max_candidates=5)
        assert len(identified) == 3
        keys = [c.candidate_identity_key for c in identified]
        assert keys == [
            "source1.md:0:c-same",
            "source1.md:5:c-same",
            "source2.md:0:c-same",
        ]


class TestCapability5RetrievalEvidenceRecording:
    def test_record_evidence_produces_faithful_retrieval_result(self):
        query = prepare_query(_make_unit("presence and calmness", id="q-7"))
        c = _make_unit("presence alone", id="c1", source="s.md", position=0)
        assessments = assess_relevance(query, [c])
        identified = identify_candidates(assessments)

        result = record_evidence(query, identified, corpus_size=10, strategy="token_overlap")
        assert result.query_unit_id == "q-7"
        assert result.has_candidates is True
        assert len(result.candidates) == 1
        assert result.corpus_size == 10
        assert "Retrieved 1 of 10 examined units" in result.note
        assert "decides nothing" in result.note


class TestRetrievalCoordinator:
    def test_coordinator_executes_sequential_capabilities_and_emits_substeps(self):
        events = []

        def observer(ev):
            events.append(ev)

        coordinator = RetrievalCoordinator(
            strategy="token_overlap",
            min_shared_terms=1,
            max_candidates=5,
            observer=observer,
        )

        query = _make_unit("presence and quiet calmness", id="q-coord")
        corpus = [
            _make_unit("quiet presence in the morning", id="c-1", source="a.md", position=0),
            _make_unit("calmness in daily routine", id="c-2", source="b.md", position=1),
            _make_unit("unrelated text", id="c-3", source="c.md", position=2),
        ]

        result = coordinator.run(query, corpus, run_id="run-test-substeps")
        assert result.has_candidates is True
        assert len(result.candidates) == 2

        # Verify started and completed phases are emitted in pairs for each capability
        phases = [(e["operation"], e.get("phase")) for e in events if e.get("kind") == "status"]
        assert phases == [
            ("query_preparation", "started"),
            ("query_preparation", "completed"),
            ("candidate_retrieval", "started"),
            ("candidate_retrieval", "completed"),
            ("relevance_assessment", "started"),
            ("relevance_assessment", "completed"),
            ("candidate_identification", "started"),
            ("candidate_identification", "completed"),
            ("retrieval_evidence_recording", "started"),
            ("retrieval_evidence_recording", "completed"),
        ]

        for ev in events:
            assert ev["stage"] == "retrieval"
            assert ev["run_id"] == "run-test-substeps"
            assert "operation" in ev
            assert "message" in ev

    def test_token_overlap_retriever_wires_observer(self):
        events = []
        retriever = TokenOverlapRetriever(
            observer=lambda ev: events.append(ev)
        )
        query = _make_unit("mind and body awareness", id="q-wire")
        corpus = [_make_unit("body awareness practice", id="c-wire")]
        res = retriever.retrieve(query, corpus)
        assert res.has_candidates is True
        phases = [(ev.get("operation"), ev.get("phase")) for ev in events]
        assert phases == [
            ("query_preparation", "started"),
            ("query_preparation", "completed"),
            ("candidate_retrieval", "started"),
            ("candidate_retrieval", "completed"),
            ("relevance_assessment", "started"),
            ("relevance_assessment", "completed"),
            ("candidate_identification", "started"),
            ("candidate_identification", "completed"),
            ("retrieval_evidence_recording", "started"),
            ("retrieval_evidence_recording", "completed"),
        ]

    def test_lancedb_vector_retriever_wires_observer_and_capabilities(self, tmp_path):
        import numpy as np
        from src.living_authenticity.database.lancedb_manager import LanceDBManager
        from src.living_authenticity.database.schema import VECTOR_DIMENSION
        from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
            LanceDBVectorRetriever,
        )

        class DeterministicEmbedder:
            def __init__(self, dimension=VECTOR_DIMENSION):
                self.dimension = dimension

            def embed(self, text):
                seed = abs(hash(text)) % 97 + 1
                vec = np.array(
                    [float((seed * (i + 1)) % 13) / 13.0 for i in range(self.dimension)],
                    dtype=np.float32,
                )
                norm = float(np.linalg.norm(vec))
                return vec / norm if norm > 0 else vec

        isolated_db = tmp_path / "test_vec_db"
        manager = LanceDBManager(db_path=str(isolated_db))
        manager.create_knowledge_vector_table()
        service = DeterministicEmbedder()

        doc1 = "The quiet morning facilitates deep reflection and research."
        manager.store(doc1, service.embed(doc1), {"source": "vault/doc1.md", "position": 2})

        events = []
        retriever = LanceDBVectorRetriever(
            embedding_service=service,
            db_manager=manager,
            observer=lambda ev: events.append(ev),
        )
        query = _make_unit("deep philosophical reflection in morning", id="q-lancedb")
        res = retriever.retrieve(query, corpus=[])
        assert res.has_candidates is True
        assert len(res.candidates) >= 1
        phases = [(ev.get("operation"), ev.get("phase")) for ev in events]
        assert phases == [
            ("query_preparation", "started"),
            ("query_preparation", "completed"),
            ("candidate_retrieval", "started"),
            ("candidate_retrieval", "completed"),
            ("relevance_assessment", "started"),
            ("relevance_assessment", "completed"),
            ("candidate_identification", "started"),
            ("candidate_identification", "completed"),
            ("retrieval_evidence_recording", "started"),
            ("retrieval_evidence_recording", "completed"),
        ]
        assert "vector_distance" in res.candidates[0].evidence_basis or res.candidates[0].evidence_basis == "vector_similarity"
        assert res.candidates[0].candidate_identity_key.startswith("vault/doc1.md:2:")
        assert "capabilities" in res.metadata

    def test_coordinator_rejects_unsupported_strategy(self):
        with pytest.raises(ValueError, match="unsupported coordinator strategy"):
            RetrievalCoordinator(strategy="vector")

    def test_coordinator_emits_failed_phase_on_exception(self):
        events = []
        coordinator = RetrievalCoordinator(
            strategy="token_overlap",
            observer=lambda ev: events.append(ev),
        )
        # Passing invalid query object causes prepare_query to raise TypeError
        with pytest.raises(TypeError):
            coordinator.run("invalid_query", [], run_id="run-fail-test")  # type: ignore[arg-type]

        failed_events = [e for e in events if e.get("phase") == "failed"]
        assert len(failed_events) >= 1
        assert failed_events[0]["operation"] == "query_preparation"
        assert failed_events[0]["run_id"] == "run-fail-test"

    def test_record_evidence_populates_metadata_when_none_supplied(self):
        query = prepare_query(_make_unit("presence and awareness", id="q-meta"))
        c1 = _make_unit("presence and awareness deeply", id="c-1")
        identified = identify_candidates(assessments=assess_relevance(query, [c1]))

        # Caller supplies metadata=None
        res = record_evidence(query, identified, corpus_size=5, strategy="token_overlap", metadata=None)
        assert isinstance(res.metadata, dict)
        assert "capabilities" in res.metadata
        assert res.metadata["query_terms_count"] == len(query.query_terms)
        assert res.metadata["candidates_count"] == len(identified)
        assert res.metadata["corpus_size"] == 5

    def test_record_evidence_handles_corpus_size_none(self):
        query = prepare_query(_make_unit("presence and awareness", id="q-none"))
        c1 = _make_unit("presence and awareness deeply", id="c-1")
        identified = identify_candidates(assessments=assess_relevance(query, [c1]))

        res = record_evidence(query, identified, corpus_size=None, strategy="token_overlap")
        assert res.corpus_size is None
        assert res.metadata.get("corpus_size_unknown") is True
        assert res.metadata.get("corpus_size_reporting") == "unknown"
        assert "unmeasured collection" in res.note

    def test_coordinator_allows_max_candidates_zero(self):
        coordinator = RetrievalCoordinator(strategy="token_overlap", max_candidates=0)
        query = _make_unit("presence and calmness", id="q-zero")
        corpus = [_make_unit("presence and calmness", id="c-zero")]
        res = coordinator.run(query, corpus)
        assert res.candidates == ()

    def test_retrieve_candidates_rejects_min_shared_terms_less_than_one(self):
        query = prepare_query(_make_unit("presence", id="q-min"))
        corpus = [_make_unit("presence", id="c-min")]
        with pytest.raises(ValueError, match="min_shared_terms must be at least 1"):
            retrieve_candidates(query, corpus, min_shared_terms=0)
        with pytest.raises(ValueError, match="min_shared_terms must be at least 1"):
            retrieve_candidates(query, corpus, min_shared_terms=-1)

    def test_coordinator_and_pipeline_support_lexical_alias(self):
        coordinator = RetrievalCoordinator(strategy="lexical")
        assert coordinator.strategy == "lexical"
        query = _make_unit("presence and calmness", id="q-lex")
        corpus = [_make_unit("presence and calmness", id="c-lex")]
        res = coordinator.run(query, corpus)
        assert len(res.candidates) == 1
        assert res.strategy == "lexical"
