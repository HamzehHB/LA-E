"""Vector-store separation contract: the index never lives in the vault."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.integration.indexing import index_units
from src.living_authenticity.knowledge.integration.vector import (
    check_vector_store_eligible,
)


class _Vector:
    def tolist(self):
        return [0.0, 1.0]


class _StubEmbedder:
    def embed_batch(self, texts):
        return [_Vector() for _ in texts]


class _StubStore:
    def __init__(self):
        self.rows: list = []

    def show_all(self):
        return list(self.rows)

    def store(self, text, vector, metadata):
        self.rows.append((text, metadata))


def test_vector_store_inside_vault_is_rejected(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    candidate = vault / "index.lancedb"
    eligible, reason, check = check_vector_store_eligible(
        str(candidate), production_vector_db="", vault_root=str(vault))
    assert eligible is False
    assert check == "vector_db"
    assert "vault" in reason


def test_index_units_preserves_provenance_and_stays_reproducible(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    before = list(vault.rglob("*"))
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )
    unit = KnowledgeUnit(
        id="ku-vault-1", source=str(vault / "note.md"),
        original_text="synthetic vault observation",
        meaning="synthetic vault observation",
        cleaned_text="synthetic vault observation",
        normalized_text="synthetic vault observation", position=0)
    store = _StubStore()
    indexed, stored = index_units([unit], _StubEmbedder(), store)
    assert (indexed, stored) == (1, 1)
    assert store.rows[0][1]["source"] == str(vault / "note.md")
    try:
        index_units([unit], _StubEmbedder(), store)
    except ValueError as exc:
        assert "already holds" in str(exc)
    else:
        raise AssertionError("second indexing must be refused")
    # Indexing wrote to the isolated store, never into the vault.
    assert list(vault.rglob("*")) == before


def test_missing_vector_path_is_rejected_before_any_indexing():
    # A missing isolated vector path is rejected explicitly instead of
    # being presented as vault-aware evidence.
    eligible, reason, check = check_vector_store_eligible("")
    assert eligible is False
    assert check == "vector_db"
    assert reason == "vector store path missing"
