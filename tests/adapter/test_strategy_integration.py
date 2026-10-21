"""Integration tests for user-selectable retrieval strategies and temporary cache lifecycle.

Verifies:
* start_run validates retrieval_mode via strategy registry;
* hybrid is rejected with validation_error (future-ready contract only);
* semantic retrieval without configured vector_db raises validation error (NO silent fallback);
* semantic retrieval with eligible vector store allocates temporary cache, retrieves candidates,
  guarantees cache cleanup, and records cache receipt in audit records;
* lexical retrieval runs with zero embeddings generated and records lexical strategy;
* cancellation and cleanup safety.
"""
from pathlib import Path
import json
import numpy as np
import pytest

from src.living_authenticity.adapter.app import AdapterError, Launcher
from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.database.schema import VECTOR_DIMENSION
from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline


class StubEmbedder:
    """Stub embedder matching LanceDB expected dimension."""

    def __init__(self, dimension=VECTOR_DIMENSION):
        self.dimension = dimension
        self.provider_name = "stub_provider"
        self.model_name = "stub_model_v1"
        self.model_version = "1.0.0"

    def embed(self, text):
        seed = abs(hash(text)) % 97 + 1
        vec = np.array(
            [float((seed * (i + 1)) % 13) / 13.0 for i in range(self.dimension)],
            dtype=np.float32,
        )
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else vec


def test_start_run_rejects_hybrid_strategy():
    """Hybrid strategy is future-ready and must be rejected at submission time."""
    launcher = Launcher(paths={"staging": {"root": ""}, "audit": {"root": ""}})
    with pytest.raises(AdapterError) as exc_info:
        launcher.start_run(
            "This is a long enough synthetic text input that meets the minimum length requirement.",
            source_mode="examples",
            retrieval_mode="hybrid",
        )
    assert exc_info.value.kind == "validation_error"
    assert "Hybrid retrieval is a planned future strategy" in str(exc_info.value)


def test_start_run_rejects_unknown_strategy():
    launcher = Launcher(paths={"staging": {"root": ""}, "audit": {"root": ""}})
    with pytest.raises(AdapterError) as exc_info:
        launcher.start_run(
            "This is a long enough synthetic text input that meets the minimum length requirement.",
            source_mode="examples",
            retrieval_mode="unknown_mode",
        )
    assert exc_info.value.kind == "validation_error"
    assert "unsupported retrieval strategy 'unknown_mode'" in str(exc_info.value)


def test_semantic_retrieval_without_vector_db_fails_closed(tmp_path):
    """Semantic retrieval MUST NOT silently fall back to lexical when vector DB is missing."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )

    with pytest.raises(AdapterError) as exc_info:
        launcher._execute_run(
            run_id="run-sem-nodb",
            input_text="Synthetic text that meets the length threshold for analytical processing.",
            mode_value="examples",
            retrieval_mode="semantic",
        )
    assert exc_info.value.kind == "validation_error"
    assert "no vector database selected" in str(exc_info.value)


def test_semantic_retrieval_with_vector_store_and_temporary_cache(tmp_path):
    """Semantic retrieval uses isolated vector store, creates temporary cache, and cleans it up."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    for cat in ("passed", "failed"):
        (audit / cat).mkdir()

    vector_dir = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(vector_dir))
    manager.create_knowledge_vector_table()

    embedder = StubEmbedder()
    doc = "Prior knowledge regarding quiet observation and presence."
    manager.store(doc, embedder.embed(doc), {"source": "prior.md"})

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )

    # Configure session destinations for vector DB and a dummy embedding model path
    launcher._session_destinations["vector_db"] = str(vector_dir)
    dummy_model = tmp_path / "model_weights"
    dummy_model.mkdir()
    launcher._session_destinations["embedding_model"] = str(dummy_model)

    # Intercept build_vector_components to inject StubEmbedder instead of disk model
    from src.living_authenticity.knowledge.integration import vector as _vector
    real_build = _vector.build_vector_components
    _vector.build_vector_components = lambda p, **kw: real_build(p, embedder=embedder, max_candidates=3)

    try:
        report = launcher._execute_run(
            run_id="run-sem-success-01",
            input_text="A completely fresh input exploring quiet observation in everyday moments.",
            mode_value="examples",
            retrieval_mode="semantic",
        )
    finally:
        _vector.build_vector_components = real_build

    assert report.accepted is True

    # Events verification: strategy selected, cache allocated, cache cleaned up
    events = launcher.events.since(0)
    stages = [e.get("stage") for e in events]
    assert "retrieval" in stages

    retrieval_events = [e for e in events if e.get("stage") == "retrieval"]
    messages = [e.get("message", "") for e in retrieval_events]

    assert any("retrieval strategy: semantic" in m for m in messages)
    assert any("temporary embedding cache allocated" in m for m in messages)
    assert any("temporary embedding cache released successfully" in m for m in messages)

    # Persistent audit verification
    audit_files = list(audit.rglob("audit.jsonl"))
    assert audit_files, "audit record must be persisted"
    records = []
    for af in audit_files:
        with open(af, "r", encoding="utf-8") as f:
            records.extend([json.loads(line) for line in f])

    run_records = [r for r in records if r.get("run_id") == "run-sem-success-01"]
    assert len(run_records) >= 1
    record = run_records[0]
    assert (
        record.get("retrieval_strategy") == "lancedb_vector"
        or record.get("retrieval_strategy") == "semantic"
    )
    assert record.get("retrieval_cache_scope_id", "").startswith(
        "cache-run-sem-success-01-"
    )
    assert record.get("retrieval_embeddings_generated", 0) >= 1
    assert record.get("retrieval_cache_cleaned_up") is True


def test_lexical_retrieval_generates_zero_embeddings(tmp_path):
    """Lexical retrieval runs without embedding model or temporary cache."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    for cat in ("passed", "failed"):
        (audit / cat).mkdir()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )

    report = launcher._execute_run(
        run_id="run-lex-success-01",
        input_text="A completely fresh input exploring quiet observation in everyday moments.",
        mode_value="examples",
        retrieval_mode="lexical",
    )
    assert report.accepted is True

    events = launcher.events.since(0)
    retrieval_events = [e for e in events if e.get("stage") == "retrieval"]
    messages = [e.get("message", "") for e in retrieval_events]

    assert any("retrieval strategy: lexical" in m for m in messages)
    assert not any("temporary embedding cache allocated" in m for m in messages)


def test_cancellation_during_semantic_run_guarantees_cache_cleanup(tmp_path):
    """When a run is cancelled, temporary cache cleanup is still guaranteed."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()

    vector_dir = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(vector_dir))
    manager.create_knowledge_vector_table()
    embedder = StubEmbedder()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._session_destinations["vector_db"] = str(vector_dir)
    dummy_model = tmp_path / "model_weights"
    dummy_model.mkdir()
    launcher._session_destinations["embedding_model"] = str(dummy_model)

    from src.living_authenticity.knowledge.integration import vector as _vector

    real_build = _vector.build_vector_components
    _vector.build_vector_components = lambda p, **kw: real_build(
        p, embedder=embedder, max_candidates=2
    )

    # Set cancellation flag before execution completes
    launcher._cancel_flags.add("run-sem-cancel")

    try:
        report = launcher._execute_run(
            run_id="run-sem-cancel",
            input_text="A completely fresh input exploring quiet observation in everyday moments.",
            mode_value="examples",
            retrieval_mode="semantic",
        )
    finally:
        _vector.build_vector_components = real_build

    events = launcher.events.since(0)
    messages = [e.get("message", "") for e in events]
    # Verify cleanup event was still emitted despite cancellation
    assert any("temporary embedding cache released successfully" in m for m in messages)


def test_cleanup_failure_emits_warning_event(tmp_path):
    """When cache cleanup encounters an error, a warning event is emitted."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()

    vector_dir = tmp_path / "isolated_vector_db"
    manager = LanceDBManager(db_path=str(vector_dir))
    manager.create_knowledge_vector_table()
    embedder = StubEmbedder()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._session_destinations["vector_db"] = str(vector_dir)
    dummy_model = tmp_path / "model_weights"
    dummy_model.mkdir()
    launcher._session_destinations["embedding_model"] = str(dummy_model)

    from src.living_authenticity.knowledge.integration import vector as _vector

    real_build = _vector.build_vector_components

    def _simulated_build(p, **kw):
        svc, st, ret = real_build(p, embedder=embedder, max_candidates=2)
        return svc, st, ret

    _vector.build_vector_components = _simulated_build

    # Monkeypatch TemporaryEmbeddingCache to simulate failure
    from src.living_authenticity.knowledge.analysis.retrieval.cache import (
        TemporaryEmbeddingCache,
    )

    real_init = TemporaryEmbeddingCache.__init__

    def _broken_cache_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self._fail_cleanup_for_testing = True

    TemporaryEmbeddingCache.__init__ = _broken_cache_init

    try:
        launcher._execute_run(
            run_id="run-cleanup-fail",
            input_text="A completely fresh input exploring quiet observation in everyday moments.",
            mode_value="examples",
            retrieval_mode="semantic",
        )
    finally:
        _vector.build_vector_components = real_build
        TemporaryEmbeddingCache.__init__ = real_init

    events = launcher.events.since(0)
    warning_events = [e for e in events if e.get("kind") == "warning"]
    warn_msgs = [e.get("message", "") for e in warning_events]
    assert any("temporary embedding cache cleanup failed" in m for m in warn_msgs)


def test_cache_cleanup_on_exception_and_safestop(tmp_path):
    """Temporary embedding cache is guaranteed to clean up on exception and safe-stop paths."""
    from src.living_authenticity.knowledge.analysis.retrieval.cache import TemporaryEmbeddingCache

    staging = tmp_path / "staging_exc"
    staging.mkdir()
    audit = tmp_path / "audit_exc"
    audit.mkdir()

    vector_dir = tmp_path / "isolated_vector_db_exc"
    manager = LanceDBManager(db_path=str(vector_dir))
    manager.create_knowledge_vector_table()
    embedder = StubEmbedder()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging)},
            "audit": {"root": str(audit)},
        },
        llm_config={"llm": {"provider": "disabled"}},
    )
    launcher._session_destinations["vector_db"] = str(vector_dir)
    dummy_model = tmp_path / "model_weights_exc"
    dummy_model.mkdir()
    launcher._session_destinations["embedding_model"] = str(dummy_model)

    from src.living_authenticity.knowledge.integration import vector as _vector
    real_build = _vector.build_vector_components
    _vector.build_vector_components = lambda p, **kw: real_build(p, embedder=embedder, max_candidates=2)

    captured_caches = []
    real_init = TemporaryEmbeddingCache.__init__

    def _spy_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        captured_caches.append(self)

    TemporaryEmbeddingCache.__init__ = _spy_init

    try:
        # 1. Exception path: simulate an unexpected exception during execution
        original_load = launcher._load_knowledge_corpus
        launcher._load_knowledge_corpus = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Unexpected load crash"))

        with pytest.raises(RuntimeError, match="Unexpected load crash"):
            launcher._execute_run(
                run_id="run-exc-test",
                input_text="A completely fresh input exploring quiet observation in everyday moments.",
                mode_value="examples",
                retrieval_mode="semantic",
            )
        assert len(captured_caches) == 1
        assert captured_caches[0].cleaned_up is True

        # 2. Safe-stop path: restore load_knowledge_corpus, LLM provider is disabled (safe stop)
        launcher._load_knowledge_corpus = original_load
        report = launcher._execute_run(
            run_id="run-safestop-test",
            input_text="A completely fresh input exploring quiet observation in everyday moments.",
            mode_value="examples",
            retrieval_mode="semantic",
        )
        assert len(captured_caches) == 2
        assert captured_caches[1].cleaned_up is True
    finally:
        _vector.build_vector_components = real_build
        TemporaryEmbeddingCache.__init__ = real_init
