"""Pinning, replay, and audit-reference tests for the bounded integration run."""
from pathlib import Path

from src.living_authenticity.knowledge.integration import (
    run_bounded_integration,
)
from tests._synthetic_synthesis import SyntheticSynthesis

SAMPLE = ("Observation:\nI observed that people often share calm morning rituals "
          "and I notice that the pattern repeats daily.\n")


def _write(directory, name, text=SAMPLE):
    target = Path(directory) / name
    target.write_text(text, encoding="utf-8")
    return target


def _run(source, staging, spy=None):
    cores = [_core()]
    synthesis = SyntheticSynthesis()
    if spy is not None:
        return run_bounded_integration(
            source, staging, guarded_roots=(), approval_reader=_yes,
            core_units=cores, max_files=1, executor=spy,
            synthesis=synthesis)
    return run_bounded_integration(
        source, staging, guarded_roots=(), approval_reader=_yes,
        core_units=cores, max_files=1, synthesis=synthesis)


def _layouts(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    return str(source), str(staging)


def _yes(_request=None):
    return "y"


CORE_SAMPLE = ("Method:\nA method for calibrating laboratory instruments "
               "with a step by step procedure.\n")


def _core():
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )

    return KnowledgeUnit(
        id="core1", source="core", original_text=CORE_SAMPLE,
        meaning=CORE_SAMPLE, cleaned_text=CORE_SAMPLE,
        normalized_text=CORE_SAMPLE,
        context="", proposed_type="", position=0,
    )


class _SpyExecutor:
    """Record exact staging_root values forwarded to execution."""

    def __init__(self):
        self.seen = []

    def execute(self, request, outcome, proposal=None, confidence=None,
                note=None, staging_root=None, schema_version=""):
        from src.living_authenticity.knowledge.governance.execution.executor import (
            ControlledExecutor,
        )

        self.seen.append(staging_root)
        return ControlledExecutor().execute(
            request, outcome, proposal, confidence, note,
            staging_root=staging_root, schema_version=schema_version or "")


def test_staging_root_pinned_across_units(tmp_path):
    source, staging = _layouts(tmp_path)
    _write(Path(source), "note.md", SAMPLE + "\n\nObservation:\nSecond calm note.\n")
    spy = _SpyExecutor()
    report = _run(source, staging, spy=spy)
    assert report.accepted is True
    assert spy.seen, "expected execution calls"
    assert all(value == staging for value in spy.seen)


def test_no_execution_after_isolation_rejection(tmp_path):
    from Config.settings import ROOT as REPOSITORY_ROOT

    source, _staging = _layouts(tmp_path)
    _write(Path(source), "note.md")
    report = run_bounded_integration(
        source, str(REPOSITORY_ROOT), guarded_roots=(),
        approval_reader=_yes)
    assert report.accepted is False
    assert report.units == ()
    assert report.audits == ()


def test_replay_reports_existing_artifact_without_overwrite(tmp_path):
    source, staging = _layouts(tmp_path)
    _write(Path(source), "note.md")
    first = _run(source, staging)
    assert any(unit.outcome == "executed" for unit in first.units)
    paths = {unit.artifact_reference for unit in first.units if unit.outcome == "executed"}
    before = {name: (Path(staging) / name).read_text(encoding="utf-8") for name in paths}
    second = _run(source, staging)
    assert any(unit.outcome == "rejected_existing_artifact" for unit in second.units)
    assert not any(unit.outcome == "executed" for unit in second.units)
    for name, text in before.items():
        assert (Path(staging) / name).read_text(encoding="utf-8") == text


def test_audit_references_are_inert_strings(tmp_path):
    source, staging = _layouts(tmp_path)
    _write(Path(source), "note.md")
    report = _run(source, staging)
    assert report.units
    assert report.audits
    assert all(isinstance(entry.source_file, str) for entry in report.units)
    assert all(isinstance(entry.artifact_reference, str) for entry in report.units)
    for audit in report.audits:
        assert isinstance(audit.artifact_reference, str)
        assert isinstance(audit.destination, str)
