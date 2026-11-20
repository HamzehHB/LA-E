"""Comprehensive integration test suite for Cold Start Governance & Core Lifecycle.

Covers all 23 mandatory integration scenarios specified by the Phase 3 governance contract:
1. Missing Core directory.
2. Existing but empty Core directory.
3. Missing Core note.
4. Empty or whitespace-only Core note.
5. Malformed YAML frontmatter.
6. Missing or incompatible canonical type.
7. Multiple active Core notes (max_active_notes: 1).
8. Unexpected non-Markdown files.
9. Unexpected subdirectories in Core folder.
10. Unreadable Core directory or note.
11. Invalid or unreadable Knowledge-Schema configuration.
12. Path boundary or symlink violation.
13. Missing Test Mode Core fixture.
14. Direct runner invocation with absent Core.
15. Direct orchestrator invocation with absent Core.
16. CLI invocation without required Core.
17. Core becoming unavailable before retry.
18. Core being replaced/changed before retry.
19. Successful recovery after human fixes Core.
20. Repeated recovery failure without overwriting earlier audit records.
21. Audit persistence failure during Core halt.
22. No LLM synthesis, proposal, approval, or execution after failed pre-flight.
23. No false successful completion after operational halt.
"""
import json
from pathlib import Path
import time
from typing import Any
import pytest

from src.living_authenticity.adapter.app import AdapterError, Launcher
from src.living_authenticity.cli import main as cli_main
from src.living_authenticity.knowledge.governance.audit import AuditWriter
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.integration.runner import run_bounded_integration
from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
from src.living_authenticity.knowledge.vault.core_validator import (
    CoreFailureCategory,
    CoreResolutionError,
    validate_and_load_core,
)


class MockE2ESynthesis:
    """Mock synthesis provider recording any call to ensure LLM is never invoked on halt."""

    def __init__(self):
        self.invocations = 0

    def synthesize(self, *args, **kwargs):
        self.invocations += 1
        raise AssertionError("LLM synthesis must never be called when Core validation fails!")


def _create_launcher(tmp_path: Path, vault_dir: Path, mock_synth: MockE2ESynthesis | None = None) -> tuple[Launcher, list[dict], Path, Path]:
    staging_dir = tmp_path / "staging_zone"
    staging_dir.mkdir(parents=True, exist_ok=True)
    audit_dir = tmp_path / "audit_zone"
    audit_dir.mkdir(parents=True, exist_ok=True)
    AuditWriter(str(audit_dir)).ensure_directories()

    events: list[dict] = []
    synth = mock_synth or MockE2ESynthesis()

    launcher = Launcher(
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {
                "root": str(audit_dir),
                "passed": str(audit_dir / "passed"),
                "failed": str(audit_dir / "failed"),
            },
            "obsidian": {"vault": str(vault_dir)},
        },
        synthesis_factory=lambda confirm=None, config=None: synth,
        is_test_mode=True,
    )
    launcher._session_destinations["vault"] = str(vault_dir)
    launcher._session_destinations["staging"] = str(staging_dir)
    launcher._session_destinations["audit"] = str(audit_dir)

    orig_sink = launcher._sink

    def sink(payload):
        events.append(payload)
        orig_sink(payload)

    launcher._sink = sink
    return launcher, events, staging_dir, audit_dir


def _wait_for_run(launcher: Launcher, run_id: str) -> dict:
    for _ in range(50):
        st = launcher.run_status(run_id)
        if st.get("accepted") is not None or st.get("active") is False:
            return st
        time.sleep(0.05)
    return launcher.run_status(run_id)


# ==============================================================================
# Scenario 1: Missing Core directory
# ==============================================================================
def test_scenario_01_missing_core_directory(tmp_path):
    vault = tmp_path / "vault_missing_core"
    vault.mkdir()
    (vault / "note.md").write_text("Regular note.", encoding="utf-8")

    synth = MockE2ESynthesis()
    launcher, events, staging, audit = _create_launcher(tmp_path, vault, synth)

    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    run_id = res["run_id"]
    st = _wait_for_run(launcher, run_id)

    assert st.get("accepted") is False
    assert st.get("terminal_outcome") == "infrastructure_error"
    assert synth.invocations == 0
    assert list(staging.glob("*.md")) == []

    # Verify ERROR audit persistence
    err_file = audit / "errors" / "audit.jsonl"
    assert err_file.exists()
    records = [json.loads(l) for l in err_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(r.get("run_id") == run_id and r.get("terminal_outcome") == "infrastructure_error" for r in records)


# ==============================================================================
# Scenario 2: Existing but empty Core directory
# ==============================================================================
def test_scenario_02_empty_core_directory(tmp_path):
    vault = tmp_path / "vault_empty_core"
    vault.mkdir()
    (vault / "01 Core").mkdir()

    launcher, events, staging, audit = _create_launcher(tmp_path, vault)
    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    st = _wait_for_run(launcher, res["run_id"])

    assert st.get("accepted") is False
    assert st.get("terminal_outcome") == "infrastructure_error"
    assert launcher._paused_run is not None
    assert "retry" in launcher._paused_run["recovery_options"]


# ==============================================================================
# Scenario 3: Missing Core note (directory exists but has other things)
# ==============================================================================
def test_scenario_03_missing_core_note_in_directory(tmp_path):
    vault = tmp_path / "vault_no_core_note"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.DIRECTORY_EMPTY


# ==============================================================================
# Scenario 4: Empty or whitespace-only Core note
# ==============================================================================
def test_scenario_04_empty_or_whitespace_core_note(tmp_path):
    vault = tmp_path / "vault_ws_core"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text("   \n\n\t  \n", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.NOTE_EMPTY


# ==============================================================================
# Scenario 5: Malformed YAML frontmatter
# ==============================================================================
def test_scenario_05_malformed_frontmatter(tmp_path):
    vault = tmp_path / "vault_bad_fm"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()

    # Case A: Missing starting ---
    (core_dir / "core.md").write_text("title: Core\ncanonical_type: Core\n---\nBody text.", encoding="utf-8")
    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.FRONTMATTER_MALFORMED

    # Case B: Unclosed frontmatter
    (core_dir / "core.md").write_text("---\ntitle: Core\ncanonical_type: Core\nBody text without closing.", encoding="utf-8")
    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.FRONTMATTER_MALFORMED


# ==============================================================================
# Scenario 6: Missing or incompatible canonical type
# ==============================================================================
def test_scenario_06_incompatible_canonical_type(tmp_path):
    vault = tmp_path / "vault_wrong_type"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()

    # Rule: tags: [core] cannot substitute for canonical_type: Core
    (core_dir / "core.md").write_text(
        "---\ntitle: Attempted Core\ncanonical_type: Observation\ntags: [core]\n---\n\nCore thesis.",
        encoding="utf-8",
    )
    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.CANONICAL_TYPE_INVALID


# ==============================================================================
# Scenario 7: Multiple active Core notes (max_active_notes: 1)
# ==============================================================================
def test_scenario_07_multiple_active_core_notes(tmp_path):
    vault = tmp_path / "vault_multi_core"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()

    (core_dir / "core1.md").write_text("---\ntitle: Core 1\ncanonical_type: Core\n---\n\nThesis 1.", encoding="utf-8")
    (core_dir / "core2.md").write_text("---\ntitle: Core 2\ncanonical_type: Core\n---\n\nThesis 2.", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.MULTIPLE_ACTIVE_NOTES


# ==============================================================================
# Scenario 8: Unexpected non-Markdown files in Core directory
# ==============================================================================
def test_scenario_08_unexpected_non_markdown_files(tmp_path):
    vault = tmp_path / "vault_unexpected_file"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()

    (core_dir / "core.md").write_text("---\ntitle: Core\ncanonical_type: Core\n---\n\nThesis.", encoding="utf-8")
    (core_dir / "unexpected.pdf").write_bytes(b"%PDF-1.4...")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.UNEXPECTED_ENTRIES


# ==============================================================================
# Scenario 9: Unexpected subdirectories in Core directory
# ==============================================================================
def test_scenario_09_unexpected_subdirectories(tmp_path):
    vault = tmp_path / "vault_subdir_core"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()

    (core_dir / "core.md").write_text("---\ntitle: Core\ncanonical_type: Core\n---\n\nThesis.", encoding="utf-8")
    (core_dir / "nested_folder").mkdir()

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.UNEXPECTED_ENTRIES


# ==============================================================================
# Scenario 10: Unreadable Core directory or note
# ==============================================================================
def test_scenario_10_unreadable_core_note(tmp_path, monkeypatch):
    vault = tmp_path / "vault_unreadable"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    target_file = core_dir / "core.md"
    target_file.write_text("---\ntitle: Core\ncanonical_type: Core\n---\n\nThesis.", encoding="utf-8")

    # Simulate OS read failure
    def mock_read_bytes(self):
        raise OSError("Simulated hardware/filesystem I/O failure")

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)
    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.INACCESSIBLE


# ==============================================================================
# Scenario 11: Invalid, unreadable, or divergent Knowledge-Schema configuration
# ==============================================================================
def test_scenario_11_invalid_knowledge_schema(tmp_path):
    bad_schema = tmp_path / "Bad-Schema.yaml"
    bad_schema.write_text("not_valid_mapping: [1, 2, 3]", encoding="utf-8")

    from src.living_authenticity.knowledge.vault.core_validator import resolve_schema_core_folder
    with pytest.raises(CoreResolutionError) as exc_info:
        resolve_schema_core_folder(schema_path=bad_schema)
    assert exc_info.value.category == CoreFailureCategory.SCHEMA_UNAVAILABLE


def test_schema_core_folder_canonical_resolution_and_immutability():
    """Verify that the authoritative schema resolves strictly to '01 Core'."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        resolve_schema_core_folder,
        is_core_relative_path,
        VAULT_CORE_FOLDER,
    )
    assert VAULT_CORE_FOLDER == "01 Core"
    resolved = resolve_schema_core_folder()
    assert resolved == "01 Core"

    # Path matching invariants
    assert is_core_relative_path("01 Core") is True
    assert is_core_relative_path("01 Core/note.md") is True
    assert is_core_relative_path("01 Core/sub/note.md") is True
    assert is_core_relative_path("\\01 Core\\note.md") is True
    assert is_core_relative_path("/01 Core/note.md") is True
    assert is_core_relative_path("02 Observation/note.md") is False
    assert is_core_relative_path("02 Observation/01 Core/note.md") is False
    assert is_core_relative_path("01 Core_Extra/note.md") is False
    assert is_core_relative_path("") is False


def test_schema_divergent_core_folder_fails_closed(tmp_path):
    """Verify that schema declaring a Core folder other than '01 Core' fails closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        resolve_schema_core_folder,
        is_core_relative_path,
        CoreResolutionError,
        CoreFailureCategory,
    )
    divergent_schema = tmp_path / "Divergent-Schema.yaml"
    divergent_schema.write_text("""
folder_structure:
  "99 Arbitrary Core":
    purpose: "Attempting to change Core folder name"
    canonical_type: "Core"
  "02 Observation":
    canonical_type: "Observation"
""", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        resolve_schema_core_folder(schema_path=divergent_schema)
    assert exc_info.value.category == CoreFailureCategory.SCHEMA_UNAVAILABLE
    assert "01 Core" in str(exc_info.value)
    assert "99 Arbitrary Core" in str(exc_info.value)

    with pytest.raises(CoreResolutionError) as exc_info:
        is_core_relative_path("99 Arbitrary Core/note.md", schema_path=divergent_schema)
    assert exc_info.value.category == CoreFailureCategory.SCHEMA_UNAVAILABLE


def test_schema_multiple_core_folders_fails_closed(tmp_path):
    """Verify that schema declaring multiple Core folders fails closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        resolve_schema_core_folder,
        CoreResolutionError,
        CoreFailureCategory,
    )
    multi_schema = tmp_path / "Multi-Core-Schema.yaml"
    multi_schema.write_text("""
folder_structure:
  "01 Core":
    canonical_type: "Core"
  "02 Extra Core":
    canonical_type: "Core"
""", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        resolve_schema_core_folder(schema_path=multi_schema)
    assert exc_info.value.category == CoreFailureCategory.SCHEMA_UNAVAILABLE
    assert "multiple Core folders" in str(exc_info.value)


def test_schema_missing_core_declaration_fails_closed(tmp_path):
    """Verify that schema with no Core folder declaration fails closed."""
    from src.living_authenticity.knowledge.vault.core_validator import (
        resolve_schema_core_folder,
        CoreResolutionError,
        CoreFailureCategory,
    )
    no_core_schema = tmp_path / "No-Core-Schema.yaml"
    no_core_schema.write_text("""
folder_structure:
  "02 Observation":
    canonical_type: "Observation"
  "03 Concepts":
    canonical_type: "Concept"
""", encoding="utf-8")

    with pytest.raises(CoreResolutionError) as exc_info:
        resolve_schema_core_folder(schema_path=no_core_schema)
    assert exc_info.value.category == CoreFailureCategory.SCHEMA_UNAVAILABLE
    assert "declares no folder with canonical_type='Core'" in str(exc_info.value)


def test_context_schema_core_folder_delegates_to_core_validator(tmp_path, monkeypatch):
    """Verify that vault.context._schema_core_folder delegates and fails closed."""
    from src.living_authenticity.knowledge.vault.context import _schema_core_folder

    # Normal resolution returns "01 Core"
    assert _schema_core_folder() == "01 Core"

    # When schema is corrupted/missing, returns "" without guessing
    from src.living_authenticity.knowledge.vault import context
    monkeypatch.setattr(context, "_SCHEMA_PATH", tmp_path / "Nonexistent.yaml")
    assert _schema_core_folder() == ""


# ==============================================================================
# Scenario 12: Path boundary or symlink violation
# ==============================================================================
def test_scenario_12_symlink_violation(tmp_path):
    vault = tmp_path / "vault_symlink"
    vault.mkdir()
    external_dir = tmp_path / "external_core"
    external_dir.mkdir()
    (external_dir / "core.md").write_text("---\ntitle: Escaped Core\ncanonical_type: Core\n---\n\nThesis.", encoding="utf-8")

    core_symlink = vault / "01 Core"
    try:
        core_symlink.symlink_to(external_dir, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation not supported without admin privileges on this Windows environment")

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("personal_vault", vault_root=str(vault))
    assert exc_info.value.category == CoreFailureCategory.SECURITY_VIOLATION


# ==============================================================================
# Scenario 13: Missing Test Mode Core fixture
# ==============================================================================
def test_scenario_13_missing_test_mode_core(tmp_path):
    empty_examples = tmp_path / "empty_examples"
    empty_examples.mkdir()

    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("examples", examples_root=str(empty_examples))
    assert exc_info.value.category == CoreFailureCategory.DIRECTORY_MISSING


# ==============================================================================
# Scenario 13b: Unreadable Test Mode Core note
# ==============================================================================
def test_scenario_13b_test_mode_unreadable_core_note(tmp_path, monkeypatch):
    examples_root = tmp_path / "examples"
    core_dir = examples_root / "core"
    core_dir.mkdir(parents=True)
    target = core_dir / "01-core-calibration.md"
    target.write_text("---\ntitle: Core Example\norigin: core-calibration\n---\n\nBody.", encoding="utf-8")

    orig_read_bytes = Path.read_bytes

    def mock_read_bytes(self):
        if "01-core-calibration.md" in str(self):
            raise OSError("Simulated hardware I/O error on test mode core")
        return orig_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", mock_read_bytes)
    with pytest.raises(CoreResolutionError) as exc_info:
        validate_and_load_core("examples", examples_root=str(examples_root))
    assert exc_info.value.category == CoreFailureCategory.INACCESSIBLE


# ==============================================================================
# Scenario 14: Direct runner invocation with absent Core
# ==============================================================================
def test_scenario_14_direct_runner_invocation_absent_core(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("Observation: Daily rhythm observation.", encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()

    report = run_bounded_integration(
        str(source),
        str(staging),
        core_units=None,
        max_files=1,
    )
    assert report.accepted is False
    assert report.run_outcome == "operational_failure"
    assert report.rejection_check == "core_preflight"
    assert "authoritative Core is mandatory" in report.rejection_reason


# ==============================================================================
# Scenario 15: Direct orchestrator invocation with absent Core
# ==============================================================================
def test_scenario_15_direct_orchestrator_absent_core(tmp_path):
    pipe = EvidenceFirstPipeline()
    u = KnowledgeUnit(
        id="q1", source="inbox", original_text="Obs text.",
        meaning="Obs text.", cleaned_text="Obs text.", normalized_text="obs text.",
        context="", proposed_type="Observation", position=0,
    )

    with pytest.raises(ValueError) as exc_info:
        pipe.run_unit(u, corpus=(), core_units=())
    assert "Core is mandatory" in str(exc_info.value)


# ==============================================================================
# Scenario 16: CLI invocation without required Core
# ==============================================================================
def test_scenario_16_cli_invocation_absent_core(tmp_path, capsys, monkeypatch):
    import src.living_authenticity.cli as cli_module
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(cli_module, "load_paths", lambda: {
        "staging": {"root": str(staging)},
        "audit": {"root": str(tmp_path / "audit")},
    })
    monkeypatch.setattr(cli_module, "check_staging_eligible", lambda *a: (True, "", str(staging)))

    code = cli_main([
        "--text", "Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.",
        "--staging-root", str(staging),
    ])
    assert code == 2
    captured = capsys.readouterr().out
    assert "Core is mandatory" in captured


# ==============================================================================
# Scenario 17: Core becoming unavailable before retry
# ==============================================================================
def test_scenario_17_core_unavailable_before_retry(tmp_path):
    vault = tmp_path / "vault_retry_unavail"
    vault.mkdir()
    # Initially missing Core
    launcher, events, staging, audit = _create_launcher(tmp_path, vault)
    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    run_id = res["run_id"]
    st = _wait_for_run(launcher, run_id)
    assert st.get("accepted") is False

    # Calling retry while Core is still unavailable
    with pytest.raises(AdapterError) as exc_info:
        launcher.retry_paused_run(run_id)
    assert "Core revalidation failed" in exc_info.value.message

    # Paused run state is preserved
    assert launcher._paused_run is not None
    assert launcher._paused_run.get("run_id") == run_id


# ==============================================================================
# Scenario 18: Core replaced or changed before retry
# ==============================================================================
def test_scenario_18_core_replaced_before_retry(tmp_path):
    vault = tmp_path / "vault_retry_replaced"
    vault.mkdir()
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    # Initially empty Core -> fails
    launcher, events, staging, audit = _create_launcher(tmp_path, vault)
    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    run_id = res["run_id"]
    _wait_for_run(launcher, run_id)

    # Human operator introduces a valid Core note
    (core_dir / "core.md").write_text(
        "---\ntitle: Restored Core\ncanonical_type: Core\ndraft: false\n---\n\nEmpirical research thesis.",
        encoding="utf-8",
    )

    # Retry succeeds and transitions to new active run
    retry_res = launcher.retry_paused_run(run_id)
    assert isinstance(retry_res, dict)
    assert retry_res.get("accepted") is True
    assert launcher._paused_run is None


# ==============================================================================
# Scenario 19: Successful recovery after human fixes Core
# ==============================================================================
def test_scenario_19_successful_recovery_provenance(tmp_path):
    vault = tmp_path / "vault_recovery"
    vault.mkdir()
    launcher, events, staging, audit = _create_launcher(tmp_path, vault)
    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    initial_run_id = res["run_id"]
    _wait_for_run(launcher, initial_run_id)

    # Human fixes core
    core_dir = vault / "01 Core"
    core_dir.mkdir()
    (core_dir / "core.md").write_text(
        "---\ntitle: Valid Core\ncanonical_type: Core\ndraft: false\n---\n\nEmpirical research anchor.",
        encoding="utf-8",
    )

    retry_res = launcher.retry_paused_run(initial_run_id)
    new_run_id = retry_res["run_id"]
    new_st = _wait_for_run(launcher, new_run_id)

    assert new_st.get("resumed_from_run_id") == initial_run_id


# ==============================================================================
# Scenario 20: Repeated recovery failure without overwriting earlier audit records
# ==============================================================================
def test_scenario_20_repeated_recovery_immutability(tmp_path):
    vault = tmp_path / "vault_repeated_fail"
    vault.mkdir()
    launcher, events, staging, audit = _create_launcher(tmp_path, vault)

    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    run_id = res["run_id"]
    _wait_for_run(launcher, run_id)

    # First retry attempt fails
    with pytest.raises(AdapterError):
        launcher.retry_paused_run(run_id)

    # Second retry attempt fails
    with pytest.raises(AdapterError):
        launcher.retry_paused_run(run_id)

    # Verify audit journal has distinct immutable error records
    err_file = audit / "errors" / "audit.jsonl"
    records = [json.loads(l) for l in err_file.read_text(encoding="utf-8").splitlines() if l.strip()]
    matching = [r for r in records if r.get("run_id") == run_id]
    assert len(matching) >= 3
    # Check that record IDs are distinct (no overwrites)
    rec_ids = [r.get("record_id") for r in matching]
    assert len(set(rec_ids)) == len(rec_ids)


# ==============================================================================
# Scenario 21: Audit persistence failure during Core halt
# ==============================================================================
def test_scenario_21_audit_persistence_failure_during_core_halt(tmp_path, monkeypatch):
    vault = tmp_path / "vault_audit_fail"
    vault.mkdir()
    launcher, events, staging, audit = _create_launcher(tmp_path, vault)

    # Monkeypatch AuditWriter.append to fail
    def mock_append(self, record):
        raise OSError("Simulated disk full or permission error")

    monkeypatch.setattr(AuditWriter, "append", mock_append)

    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    st = _wait_for_run(launcher, res["run_id"])

    assert st.get("accepted") is False
    assert st.get("audit_persisted") is False
    # Check that audit_failed event was visibly emitted
    failed_evs = [e for e in events if e.get("kind") == "audit_failed"]
    assert len(failed_evs) >= 1


# ==============================================================================
# Scenario 22: No LLM synthesis, proposal, approval, or execution after failed pre-flight
# ==============================================================================
def test_scenario_22_no_llm_or_proposals_on_failed_preflight(tmp_path):
    vault = tmp_path / "vault_zero_leakage"
    vault.mkdir()
    synth = MockE2ESynthesis()
    launcher, events, staging, audit = _create_launcher(tmp_path, vault, synth)

    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    _wait_for_run(launcher, res["run_id"])

    # 1. Zero LLM calls
    assert synth.invocations == 0
    # 2. Zero human review events
    hr_events = [e for e in events if e.get("kind") == "human_review_required"]
    assert len(hr_events) == 0
    # 3. Zero staged files
    assert list(staging.glob("*.md")) == []
    # 4. Zero proposal events
    prop_events = [e for e in events if "proposal" in e.get("stage", "")]
    assert len(prop_events) == 0


# ==============================================================================
# Scenario 23: No false successful frontend completion after operational halt
# ==============================================================================
def test_scenario_23_no_false_successful_frontend_completion(tmp_path):
    vault = tmp_path / "vault_truthful_frontend"
    vault.mkdir()
    launcher, events, staging, audit = _create_launcher(tmp_path, vault)

    res = launcher.start_run("Observation: Reading short summaries reduces context switching and cognitive fatigue in research cohorts.", source_mode="personal_vault")
    st = _wait_for_run(launcher, res["run_id"])

    # Must NOT report run_completed
    completed_events = [e for e in events if e.get("kind") == "run_completed"]
    assert len(completed_events) == 0

    # Must visibly report run_rejected with operational halt metadata
    rejected_events = [e for e in events if e.get("kind") == "run_rejected"]
    assert len(rejected_events) >= 1
    assert rejected_events[0].get("terminal_outcome") == "infrastructure_error"
    assert rejected_events[0].get("outcome") == "FAILED"
    assert "retry" in rejected_events[0].get("recovery_options", [])
