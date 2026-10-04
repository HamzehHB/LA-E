"""End-to-end example workflow over the tracked synthetic example inputs.

These tests exercise the contract that
``examples/bounded_local_integration/README.md`` documents, using only
synthetic example inputs and a temporary isolated staging directory.
No project data and no persistent-data location is read or written.
"""
from pathlib import Path

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.integration import run_bounded_integration
from tests._synthetic_synthesis import FailingSynthesis, SyntheticSynthesis

EXAMPLE_INPUT = (
    Path(__file__).resolve().parents[2]
    / "examples" / "bounded_local_integration" / "input"
)


def _unit(unit_id, text, position=0):
    return KnowledgeUnit(
        id=unit_id, source="example-corpus", original_text=text,
        meaning=text, cleaned_text=text, normalized_text=text,
        context="", proposed_type="", position=position,
    )


def _corpus():
    """Explicit in-memory corpus; never discovered from the source root."""
    return [_unit(
        "corpus-1",
        "A ledger of unrelated vocabulary about tide charts and harbour "
        "moorings for the synthetic example corpus.",
    )]


def _core_units():
    """Explicit approved Core units; unresolved Core forces NEEDS_REVIEW."""
    return [_unit(
        "core-1",
        "Quantum calibration ledger for laboratory balances and reagent "
        "inventory in the synthetic example Core set.",
    )]


def _approve_all(_request=None):
    return "y"


def _reject_all(_request=None):
    return "n"


def test_example_input_directory_is_synthetic_and_present():
    assert EXAMPLE_INPUT.is_dir()
    names = sorted(p.name for p in EXAMPLE_INPUT.iterdir())
    assert names == [
        "01-observation-attention.md",
        "02-observation-attention-repeated.md",
        "03-method-calibration.md",
        "04-ambiguous-note.txt",
    ]


def test_example_run_reaches_controlled_execution(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    synthesis = SyntheticSynthesis()
    report = run_bounded_integration(
        str(EXAMPLE_INPUT), str(staging), guarded_roots=(),
        approval_reader=_approve_all, corpus=_corpus(),
        core_units=_core_units(), max_files=1, synthesis=synthesis,
    )
    assert synthesis.calls >= 1, "the formal LLM stage must run"
    assert report.accepted is True
    assert report.files_processed == 1
    assert report.units, "expected at least one analysed knowledge unit"
    assert any(unit.outcome == "executed" for unit in report.units)
    written = sorted(p.name for p in staging.iterdir())
    assert written, "approved CREATE artifacts must land in staging"
    for name in written:
        assert name.endswith(".md")
    # Audit records exist and their path fields stay inert strings.
    assert report.audits
    for audit in report.audits:
        assert isinstance(audit.artifact_reference, str)
        assert "/" not in audit.artifact_reference
        assert "\\" not in audit.artifact_reference


def test_example_inputs_are_never_written_to(tmp_path):
    before = {
        p.name: p.read_text(encoding="utf-8") for p in EXAMPLE_INPUT.iterdir()
    }
    staging = tmp_path / "staging"
    staging.mkdir()
    run_bounded_integration(
        str(EXAMPLE_INPUT), str(staging), guarded_roots=(),
        approval_reader=_approve_all, corpus=_corpus(),
        core_units=_core_units(), max_files=1,
        synthesis=SyntheticSynthesis(),
    )
    after = {
        p.name: p.read_text(encoding="utf-8") for p in EXAMPLE_INPUT.iterdir()
    }
    assert after == before


def test_rejection_prevents_execution_for_every_unit(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    report = run_bounded_integration(
        str(EXAMPLE_INPUT), str(staging), guarded_roots=(),
        approval_reader=_reject_all, corpus=_corpus(),
        core_units=_core_units(), max_files=2,
        synthesis=SyntheticSynthesis(),
    )
    assert report.accepted is True
    assert report.units
    assert all(unit.outcome != "executed" for unit in report.units)
    assert list(staging.iterdir()) == []


def test_max_files_bound_is_respected(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    report = run_bounded_integration(
        str(EXAMPLE_INPUT), str(staging), guarded_roots=(),
        approval_reader=_reject_all, corpus=_corpus(),
        core_units=_core_units(), max_files=2,
        synthesis=SyntheticSynthesis(),
    )
    assert report.files_seen == 4
    assert report.files_processed == 2
    assert report.files_skipped == 2


def test_no_provider_means_no_candidate_and_no_execution(tmp_path):
    """Mandatory stage: an unavailable provider produces no candidate."""
    staging = tmp_path / "staging"
    staging.mkdir()
    report = run_bounded_integration(
        str(EXAMPLE_INPUT), str(staging), guarded_roots=(),
        approval_reader=_approve_all, corpus=_corpus(),
        core_units=_core_units(), max_files=1,
        synthesis=FailingSynthesis(),
    )
    assert report.accepted is True
    assert report.units
    assert all(unit.outcome == "llm_safe_stop" for unit in report.units)
    assert all(unit.reason == "llm_unavailable" for unit in report.units)
    assert list(staging.iterdir()) == [], "no candidate may reach staging"
    assert report.audits == (), "no approval may be requested or recorded"
