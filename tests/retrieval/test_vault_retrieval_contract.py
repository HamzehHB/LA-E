"""Vault retrieval contract: isolated index, provenance, explicit gaps."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.orchestration.orchestrator import (
    EvidenceFirstPipeline,
)
from src.living_authenticity.knowledge.vault.context import read_vault_units


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_vault_units_index_and_retrieve_without_writing_vault(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "01 Core" / "attention.md",
           "# Attention Observation\n\n"
           "Attention observation about focus and capacity.")
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    pipeline = EvidenceFirstPipeline()
    units, discovered, skipped = read_vault_units(str(vault), pipeline,
                                                 max_files=10)
    assert discovered == 1
    assert skipped == 0
    assert units
    assert all("attention.md" in unit.source for unit in units)
    # Retrieval over vault-derived units is provenance-bearing evidence:
    # query shares content tokens with the vault note.
    query = KnowledgeUnit(
        id="ku-query", source="synthetic-query",
        original_text="Attention observation about focus.",
        meaning="Attention observation about focus.",
        cleaned_text="Attention observation about focus.",
        normalized_text="Attention observation about focus.",
        position=0,
    )
    result = pipeline.retriever.retrieve(query, units)
    assert result.candidates
    assert all(c.source for c in result.candidates)
    # The vault itself was never written.
    after = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert before == after


def test_empty_vault_reports_unavailable_instead_of_fake_evidence(tmp_path):
    vault = tmp_path / "empty-vault"
    vault.mkdir()
    pipeline = EvidenceFirstPipeline()
    units, discovered, skipped = read_vault_units(str(vault), pipeline,
                                                 max_files=10)
    assert units == []
    assert discovered == 0
    # Callers must surface this as unavailable, never as silent evidence.
    assert skipped >= 0
