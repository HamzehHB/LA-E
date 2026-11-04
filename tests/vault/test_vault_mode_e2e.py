"""End-to-End Vault Mode Integration and Verification Suite.



Exercises the real application pipeline against an authentic synthetic

Obsidian Vault fixture containing:

- Notes at the root

- Notes across multiple nested subfolders

- Persian and English content

- Unicode filenames

- Empty notes

- Duplicate and overlapping notes

- Non-markdown attachments (.png, .csv)

- .obsidian configuration directory with plugins and README

- More than 10 notes (verifying no silent 10-file truncation)



Verifies:

1. Vault selection & path validation

2. Recursive discovery across subfolders

3. Correct file counts and no 10-file truncation

4. Correct exclusion of .obsidian and hidden directories

5. Subfolder scope selection

6. Ingestion, chunking, and provenance preservation

7. Persian & Unicode text handling

8. Empty file handling

9. Classification & proposal generation (CREATE, DO_NOT_IMPORT, NEEDS_REVIEW)

10. Human review & approval boundaries

11. Controlled execution strictly to staging (zero vault mutation)

12. Strict read-only Vault protection before and after execution

"""

from __future__ import annotations



import os

from pathlib import Path

import time

import pytest



from Config.settings import ROOT as REPOSITORY_ROOT

from src.living_authenticity.adapter.app import Launcher

from src.living_authenticity.adapter.picker import (

    STATUS_CONFIGURED,

    STATUS_INVALID,

    validate_purpose,

)

from src.living_authenticity.knowledge.governance.audit.persistence import AuditWriter

from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

from src.living_authenticity.knowledge.vault.context import (

    discover_vault_files,

    read_vault_context,

    read_vault_units,

    resolve_vault_core_root,

    snapshot_vault,

    diff_manifests,

)

from src.living_authenticity.llm import parse_llm_result

from tests._synthetic_synthesis import synthetic_raw





class VaultE2ESynthesis:

    """Deterministic synthesis double for Vault Mode E2E tests."""



    name = "vault_e2e"



    def synthesize(self, query=None, proposal=None, classification=None,

                   retrieval=None, comparisons=None, relation=None,

                   core=None, confidence=None, filter_outcome=None,

                   vault_context=None):

        title = "Synthesized Vault Knowledge Note"

        body = "Empirical synthesis derived in authentic Vault Mode."

        raw = synthetic_raw(title=title, body=body)

        result = parse_llm_result(raw)

        return (result, "vault e2e prompt", "0" * 64)





def create_synthetic_obsidian_vault(root: Path) -> dict[str, Path]:

    """Create a rich, authentic synthetic Obsidian Vault fixture."""

    root.mkdir(parents=True, exist_ok=True)



    # 1. Root-level notes

    root_note_1 = root / "welcome-note.md"

    root_note_1.write_text(

        "---\ntitle: Welcome Note\ntags: [vault, root]\n---\n\n"

        "Welcome to the synthetic research vault for LivingAuthenticity.\n"

        "This note documents initial research thoughts and general orientation.\n",

        encoding="utf-8",

    )



    root_note_persian = root / "یادداشت_پژوهشی_ریشه.md"

    root_note_persian.write_text(

        "---\ntitle: یادداشت پژوهشی\ntags: [پژوهش, ریشه]\n---\n\n"

        "این یک یادداشت پژوهشی معتبر به زبان فارسی در ریشه مخزن دانش است.\n"

        "الگوهای تمرکز و توجه پایدار در طول روزهای کاری بررسی می‌شوند.\n",

        encoding="utf-8",

    )



    root_empty = root / "empty-draft.md"

    root_empty.write_text("", encoding="utf-8")



    # 2. Schema Core directory ("01 Core")

    core_dir = root / "01 Core"

    core_dir.mkdir(parents=True, exist_ok=True)



    core_note_1 = core_dir / "core-epistemology.md"

    core_note_1.write_text(

        "---\ntitle: Core Epistemology\ncanonical_type: Core\n---\n\n"

        "Methodological foundation: All knowledge must trace back to authentic empirical observations.\n"

        "Epistemic rigor requires distinguishing similarity from identity and analysis from decision.\n",

        encoding="utf-8",

    )



    core_note_persian = core_dir / "اصول_معرفت_شناختی.md"

    core_note_persian.write_text(

        "---\ntitle: اصول معرفت‌شناختی\ncanonical_type: Core\n---\n\n"

        "اصل بنیادین: هیچ تغییر خودکاری در دانش بنیادین بدون نظارت مستقیم انسان مجاز نیست.\n"

        "شواهد تجربی و تحلیل انتقادی مبنای ارزیابی گزاره‌های جدید هستند.\n",

        encoding="utf-8",

    )



    # 3. Observation directory ("02 Observation")

    obs_dir = root / "02 Observation"

    obs_dir.mkdir(parents=True, exist_ok=True)



    obs_nested_dir = obs_dir / "field_notes"

    obs_nested_dir.mkdir(parents=True, exist_ok=True)



    obs_note_1 = obs_dir / "daily-rhythms.md"

    obs_note_1.write_text(

        "---\ntitle: Daily Rhythms\ntype: Observation\n---\n\n"

        "Observation on daily focus patterns and morning routine consistency in research cohorts.\n"

        "Participants report higher cognitive endurance when complex tasks begin before midday.\n",

        encoding="utf-8",

    )



    obs_note_nested = obs_nested_dir / "deep-field-observation.md"

    obs_note_nested.write_text(

        "---\ntitle: Deep Field Observation\ntype: Observation\n---\n\n"

        "Detailed field note recording deep contextual immersion during collaborative writing sessions.\n"

        "Cross-talk interruptions degraded analytical synthesis velocity significantly.\n",

        encoding="utf-8",

    )



    obs_duplicate = obs_dir / "duplicate-target.md"

    obs_duplicate.write_text(

        "---\ntitle: Duplicate Target\ntype: Observation\n---\n\n"

        "Exact identical overlap statement: researchers often experience fatigue after four hours of continuous writing.\n",

        encoding="utf-8",

    )



    # 4. Concepts directory ("03 Concepts" with deep nesting)

    concepts_dir = root / "03 Concepts"

    deep_concepts_dir = concepts_dir / "cognitive" / "memory" / "retention"

    deep_concepts_dir.mkdir(parents=True, exist_ok=True)



    concept_1 = concepts_dir / "working-memory.md"

    concept_1.write_text(

        "---\ntitle: Working Memory\ntype: Concept\n---\n\n"

        "Conceptual definition of working memory constraints during multidimensional knowledge retrieval.\n",

        encoding="utf-8",

    )



    concept_2 = concepts_dir / "spaced-repetition.md"

    concept_2.write_text(

        "---\ntitle: Spaced Repetition\ntype: Concept\n---\n\n"

        "Analysis of retention decay curves and optimal review interval spacing for complex theoretical models.\n",

        encoding="utf-8",

    )



    concept_deep = deep_concepts_dir / "micro-retention.md"

    concept_deep.write_text(

        "---\ntitle: Micro Retention\ntype: Concept\n---\n\n"

        "Deeply nested micro-concept examining transient attention capture during brief digital interruptions.\n",

        encoding="utf-8",

    )



    concept_persian = concepts_dir / "مفهوم_یادگیری_معنادار.md"

    concept_persian.write_text(

        "---\ntitle: مفهوم یادگیری معنادار\ntype: Concept\n---\n\n"

        "یادگیری معنادار زمانی رخ می‌دهد که اطلاعات جدید با چارچوب‌های شناختی موجود پیوند برقرار کنند.\n",

        encoding="utf-8",

    )



    # 5. Non-markdown attachments

    (obs_dir / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")

    (concepts_dir / "metrics.csv").write_text("id,metric,value\n1,retention,0.85\n", encoding="utf-8")



    # 6. .obsidian configuration folder (must be strictly ignored)

    obsidian_dir = root / ".obsidian"

    obsidian_plugins_dir = obsidian_dir / "plugins" / "custom-helper"

    obsidian_plugins_dir.mkdir(parents=True, exist_ok=True)



    (obsidian_dir / "app.json").write_text('{"theme": "obsidian", "livePreview": true}', encoding="utf-8")

    (obsidian_dir / "workspace.json").write_text('{"main": {"id": "wk-123"}}', encoding="utf-8")

    (obsidian_plugins_dir / "manifest.json").write_text('{"id": "custom-helper", "version": "1.0.0"}', encoding="utf-8")

    (obsidian_plugins_dir / "README.md").write_text("# Plugin Readme\nDo not ingest as knowledge note.\n", encoding="utf-8")



    # 7. Hidden .trash folder

    trash_dir = root / ".trash"

    trash_dir.mkdir(parents=True, exist_ok=True)

    (trash_dir / "old-deleted-note.md").write_text("Deleted note in trash.", encoding="utf-8")



    # Return dictionary of key paths

    return {

        "root": root,

        "welcome": root_note_1,

        "persian_root": root_note_persian,

        "empty": root_empty,

        "core_dir": core_dir,

        "core_1": core_note_1,

        "core_persian": core_note_persian,

        "obs_dir": obs_dir,

        "obs_1": obs_note_1,

        "obs_nested": obs_note_nested,

        "obs_duplicate": obs_duplicate,

        "concepts_dir": concepts_dir,

        "concept_1": concept_1,

        "concept_2": concept_2,

        "concept_deep": concept_deep,

        "concept_persian": concept_persian,

        "obsidian_dir": obsidian_dir,

    }





def test_vault_discovery_and_bounds_with_synthetic_vault(tmp_path):

    """Test 1: Recursive discovery, >10 notes (no 10-file limit), .obsidian exclusion."""

    vault_dir = tmp_path / "research_vault"

    vault_paths = create_synthetic_obsidian_vault(vault_dir)



    # 1. Discover files

    files, skipped = discover_vault_files(str(vault_dir))



    # Total legitimate markdown notes in fixture:

    # 3 (root) + 2 (01 Core) + 3 (02 Observation) + 4 (03 Concepts) = 12 notes.

    file_names = [p.name for p in files]

    assert len(files) == 12, f"Expected 12 notes, got {len(files)}: {file_names}"

    assert len(files) > 10, "Proves 10-file limitation no longer truncates vault discovery"



    # Verify .obsidian, .trash, and non-markdown files are excluded

    for p in files:

        rel_parts = p.relative_to(vault_dir).parts

        assert not any(part.startswith(".") for part in rel_parts), f"Hidden path ingested: {p}"

        assert p.suffix.lower() == ".md", f"Non-markdown file ingested: {p}"

        assert ".obsidian" not in str(p)

        assert ".trash" not in str(p)



    # Verify nested and deeply nested notes were discovered

    assert "deep-field-observation.md" in file_names

    assert "micro-retention.md" in file_names

    assert "یادداشت_پژوهشی_ریشه.md" in file_names

    assert "اصول_معرفت_شناختی.md" in file_names

    assert "مفهوم_یادگیری_معنادار.md" in file_names



    # Verify Core folder resolution

    core_root = resolve_vault_core_root(str(vault_dir))

    assert core_root != ""

    assert Path(core_root).name == "01 Core"



    # 2. Subfolder scope selection

    subfolder_files, _ = discover_vault_files(str(vault_paths["obs_dir"]))

    subfolder_names = [p.name for p in subfolder_files]

    assert len(subfolder_files) == 3

    assert set(subfolder_names) == {

        "daily-rhythms.md",

        "deep-field-observation.md",

        "duplicate-target.md",

    }





def test_vault_reading_and_provenance(tmp_path):

    """Test 2: Real pipeline ingestion, Persian/Unicode text, empty file handling."""

    vault_dir = tmp_path / "research_vault"

    create_synthetic_obsidian_vault(vault_dir)



    pipeline = EvidenceFirstPipeline()

    units, count, skipped = read_vault_units(str(vault_dir), pipeline)



    assert count == 12

    assert len(units) > 0



    # Verify provenance on extracted units

    for unit in units:

        assert unit.source.startswith(str(vault_dir))

        assert unit.source.endswith(".md")

        assert unit.id.startswith("ku-")

        assert unit.original_text != "" or unit.meaning != "" or unit.cleaned_text != ""



    # Verify Persian units were extracted cleanly

    persian_units = [u for u in units if "یادداشت" in u.source or "اصول" in u.source or "مفهوم" in u.source]

    assert len(persian_units) >= 3

    persian_texts = " ".join(u.cleaned_text for u in persian_units)

    assert "فارسی" in persian_texts or "معرفت‌شناختی" in persian_texts or "یادگیری" in persian_texts





def test_vault_mode_end_to_end_create_and_readonly(tmp_path):

    """Test 3: Full application workflow in Vault Mode:

    - Candidate distinct from existing vault notes & core -> CREATE

    - Human approves

    - Controlled execution writes strictly to staging

    - Vault is completely unmutated (diff_manifests before and after is clean)

    """

    vault_dir = tmp_path / "my_obsidian_vault"

    create_synthetic_obsidian_vault(vault_dir)



    staging_dir = tmp_path / "staging_zone"

    staging_dir.mkdir(parents=True, exist_ok=True)



    audit_dir = tmp_path / "audit_zone"

    audit_dir.mkdir(parents=True, exist_ok=True)

    writer = AuditWriter(str(audit_dir))

    writer.ensure_directories()



    # Capture initial vault snapshot

    before_snapshot = snapshot_vault(str(vault_dir))

    assert before_snapshot["files_discovered"] == 12



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

        synthesis_factory=lambda confirm=None, config=None: VaultE2ESynthesis(),

        is_test_mode=True,  # Authorize test-output exception in tests/

    )

    launcher._approval_reader = lambda: "y"



    # Set session destination for vault

    launcher._session_destinations["vault"] = str(vault_dir)

    launcher._session_destinations["staging"] = str(staging_dir)

    launcher._session_destinations["audit"] = str(audit_dir)



    # Novel input distinct from all vault notes and core epistemology

    novel_input = (

        "Observation: Urban bicyclists frequently navigate dedicated green corridors "

        "faster during evening commutes than motor vehicle traffic along parallel boulevards."

    )



    res = launcher.start_run(

        novel_input,

        source_mode="personal_vault",

        retrieval_mode="lexical",

        staging_language="en",

        audit_language="en",

    )

    run_id = res["run_id"] if isinstance(res, dict) else str(res)



    # Await completion

    for _ in range(50):

        status = launcher.run_status(run_id)

        if status.get("accepted") is not None or status.get("active") is False:

            break

        time.sleep(0.1)



    status = launcher.run_status(run_id)

    assert status.get("accepted") is True, f"Run failed with status: {status}"

    assert status.get("executed") == 1, f"Expected executed=1, got {status}"

    assert status.get("held") == 0



    # Verify Controlled Execution wrote strictly to staging

    staged_files = list(staging_dir.glob("*.md"))

    assert len(staged_files) == 1, f"Expected 1 staged note, got {len(staged_files)}"

    staged_text = staged_files[0].read_text(encoding="utf-8")

    assert "Empirical synthesis derived in authentic Vault Mode" in staged_text



    # Verify Audit Persistence

    passed_audit = list((audit_dir / "passed").glob("*.jsonl"))

    assert len(passed_audit) >= 1



    # Verify Strict Read-Only Vault Protection:

    after_snapshot = snapshot_vault(str(vault_dir))

    diff = diff_manifests(before_snapshot, after_snapshot)

    assert diff["stale"] is False, f"Vault was mutated during execution: {diff}"

    assert diff["added"] == []

    assert diff["modified"] == []

    assert diff["removed"] == []





def test_vault_mode_duplicate_detection(tmp_path):

    """Test 4: Input with identical content to a vault note triggers DO_NOT_IMPORT."""

    import json

    vault_dir = tmp_path / "my_obsidian_vault"

    create_synthetic_obsidian_vault(vault_dir)



    staging_dir = tmp_path / "staging_zone"

    staging_dir.mkdir(parents=True, exist_ok=True)

    audit_dir = tmp_path / "audit_zone"

    audit_dir.mkdir(parents=True, exist_ok=True)

    AuditWriter(str(audit_dir)).ensure_directories()



    recorded_events: list[dict] = []

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

        synthesis_factory=lambda confirm=None, config=None: VaultE2ESynthesis(),

        is_test_mode=True,

    )

    orig_sink = launcher._sink

    def intercepting_sink(payload):

        recorded_events.append(payload)

        orig_sink(payload)

    launcher._sink = intercepting_sink



    launcher._approval_reader = lambda: "acknowledge"

    launcher._session_destinations["vault"] = str(vault_dir)

    launcher._session_destinations["staging"] = str(staging_dir)

    launcher._session_destinations["audit"] = str(audit_dir)



    # Identical text from duplicate-target.md in vault

    duplicate_input = (

        "Exact identical overlap statement: researchers often experience fatigue after four hours of continuous writing."

    )



    res = launcher.start_run(

        duplicate_input,

        source_mode="personal_vault",

        retrieval_mode="lexical",

    )

    run_id = res["run_id"] if isinstance(res, dict) else str(res)



    for _ in range(50):

        status = launcher.run_status(run_id)

        if status.get("accepted") is not None or status.get("active") is False:

            break

        time.sleep(0.1)



    status = launcher.run_status(run_id)

    assert status.get("accepted") is True, f"Run failed with status: {status}"

    assert status.get("held") == 1, f"Expected held=1 for DO_NOT_IMPORT, got {status}"

    assert status.get("executed") == 0

    # DO_NOT_IMPORT must not write any staging file

    assert list(staging_dir.glob("*.md")) == []



    # Prove proposal action is explicitly DO_NOT_IMPORT

    hr_events = [ev for ev in recorded_events if ev.get("kind") == "human_review_required"]

    assert len(hr_events) >= 1

    assert hr_events[0]["proposal_action"] == "DO_NOT_IMPORT"

    assert hr_events[0]["is_executable"] is False



    # Prove terminal audit record in audit/failed/ reflects DO_NOT_IMPORT

    failed_audit = audit_dir / "failed" / "audit.jsonl"

    assert failed_audit.exists()

    records = [json.loads(line) for line in failed_audit.read_text(encoding="utf-8").strip().splitlines() if line.strip()]

    matching = [r for r in records if r.get("run_id") == run_id]

    assert len(matching) == 1

    assert matching[0]["proposal_action"] == "DO_NOT_IMPORT"

    assert matching[0]["terminal_outcome"] in ("held_non_executable", "held")

    assert matching[0]["approval_approved"] is False

    assert matching[0]["execution_executed"] is False





def test_vault_mode_empty_core_produces_needs_review(tmp_path):

    """Test 5: Vault with empty 01 Core produces NEEDS_REVIEW with clear explanation."""

    vault_dir = tmp_path / "empty_core_vault"

    vault_dir.mkdir(parents=True, exist_ok=True)

    (vault_dir / "01 Core").mkdir(parents=True, exist_ok=True)  # empty Core folder

    (vault_dir / "note.md").write_text("General observation note about research.", encoding="utf-8")



    staging_dir = tmp_path / "staging_zone"

    staging_dir.mkdir(parents=True, exist_ok=True)

    audit_dir = tmp_path / "audit_zone"

    audit_dir.mkdir(parents=True, exist_ok=True)

    AuditWriter(str(audit_dir)).ensure_directories()



    recorded_events: list[dict] = []

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

        synthesis_factory=lambda confirm=None, config=None: VaultE2ESynthesis(),

        is_test_mode=True,

    )

    launcher._approval_reader = lambda: "acknowledge"



    orig_sink = launcher._sink

    def intercepting_sink(payload):

        recorded_events.append(payload)

        orig_sink(payload)

    launcher._sink = intercepting_sink



    launcher._session_destinations["vault"] = str(vault_dir)

    launcher._session_destinations["staging"] = str(staging_dir)

    launcher._session_destinations["audit"] = str(audit_dir)



    input_text = "Observation: Reading short summaries reduces context switching in daily engineering workflows."



    res = launcher.start_run(

        input_text,

        source_mode="personal_vault",

        retrieval_mode="lexical",

    )

    run_id = res["run_id"] if isinstance(res, dict) else str(res)



    for _ in range(50):

        status = launcher.run_status(run_id)

        if status.get("accepted") is not None or status.get("active") is False:

            break

        time.sleep(0.1)



    import json

    status = launcher.run_status(run_id)

    assert status.get("accepted") is True, f"Run failed with status: {status}"

    assert status.get("held") == 1, f"Expected held=1 for NEEDS_REVIEW, got {status}"

    assert status.get("executed") == 0

    # Verify no staging file was written (non-executable NEEDS_REVIEW)

    assert list(staging_dir.glob("*.md")) == []



    # Verify proposal action is explicitly NEEDS_REVIEW and NOT DO_NOT_IMPORT

    hr_events = [ev for ev in recorded_events if ev.get("kind") == "human_review_required"]

    assert len(hr_events) >= 1

    assert hr_events[0]["proposal_action"] == "NEEDS_REVIEW"

    assert hr_events[0]["proposal_action"] != "DO_NOT_IMPORT"

    assert hr_events[0]["is_executable"] is False



    # Verify clear explanation event was emitted

    core_status_events = [

        ev for ev in recorded_events

        if ev.get("kind") == "status" and "no approved Core units found" in ev.get("message", "")

    ]

    assert len(core_status_events) >= 1

    assert "01 Core" in core_status_events[0]["message"]



    # Verify terminal audit record in audit/failed/ reflects NEEDS_REVIEW

    failed_audit = audit_dir / "failed" / "audit.jsonl"

    assert failed_audit.exists()

    records = [json.loads(line) for line in failed_audit.read_text(encoding="utf-8").strip().splitlines() if line.strip()]

    matching = [r for r in records if r.get("run_id") == run_id]

    assert len(matching) == 1

    assert matching[0]["proposal_action"] == "NEEDS_REVIEW"

    assert matching[0]["terminal_outcome"] in ("held_non_executable", "held")

    assert matching[0]["storage_category"] == "failed"

    assert matching[0]["approval_approved"] is False

    assert matching[0]["execution_executed"] is False







def test_vault_mode_user_selection_through_application(tmp_path):

    """Test 6 (Priority A - Path B): Vault folder selected interactively through application.



    Proves that an unconfigured Launcher dynamically transitions to Vault Mode when

    pick_folder('vault') is invoked and backend validates the selected directory.

    """

    vault_dir = tmp_path / "interactive_vault"

    create_synthetic_obsidian_vault(vault_dir)



    staging_dir = tmp_path / "staging_zone"

    staging_dir.mkdir(parents=True, exist_ok=True)

    audit_dir = tmp_path / "audit_zone"

    audit_dir.mkdir(parents=True, exist_ok=True)

    AuditWriter(str(audit_dir)).ensure_directories()



    def custom_picker(purpose: str):

        if purpose == "vault":

            return (STATUS_CONFIGURED, str(vault_dir), "")

        if purpose == "staging":

            return (STATUS_CONFIGURED, str(staging_dir), "")

        if purpose == "audit":

            return (STATUS_CONFIGURED, str(audit_dir), "")

        return (STATUS_INVALID, "", "unknown")



    # 1. Launcher starts in unconfigured state (no obsidian vault in paths)

    launcher = Launcher(

        paths={

            "staging": {"root": str(staging_dir)},

            "audit": {

                "root": str(audit_dir),

                "passed": str(audit_dir / "passed"),

                "failed": str(audit_dir / "failed"),

            },

        },

        picker=custom_picker,

        synthesis_factory=lambda confirm=None, config=None: VaultE2ESynthesis(),

        is_test_mode=True,

    )



    # Initially unconfigured

    assert launcher.session_destination("vault") == ""

    initial_state = launcher.state()

    assert initial_state["modes"]["source_mode"] == "examples"

    assert initial_state["folders"]["vault"]["configured"] is False



    # 2. User invokes folder selection for vault through application control

    status, disp, reason = launcher.pick_folder("vault")

    assert status == STATUS_CONFIGURED

    assert disp == vault_dir.name

    assert reason == ""



    # 3. Application visibly confirms selected Vault in state

    assert launcher.session_destination("vault") == str(vault_dir)

    updated_state = launcher.state()

    assert updated_state["modes"]["source_mode"] == "personal_vault"

    assert updated_state["folders"]["vault"]["configured"] is True

    assert updated_state["folders"]["vault"]["display"] == vault_dir.name





def test_vault_mode_selected_scope_used_by_subsequent_run(tmp_path):

    """Test 7 (Priority A - Path C): Run actually ingests from the selected Vault scope.



    Proves that a subsequent run reads notes from the selected vault, constructs

    provenance pointing to those files, and leaves the vault unmutated.

    """

    vault_dir = tmp_path / "scoped_vault"

    create_synthetic_obsidian_vault(vault_dir)



    staging_dir = tmp_path / "staging_zone"

    staging_dir.mkdir(parents=True, exist_ok=True)

    audit_dir = tmp_path / "audit_zone"

    audit_dir.mkdir(parents=True, exist_ok=True)

    AuditWriter(str(audit_dir)).ensure_directories()



    def custom_picker(purpose: str):

        if purpose == "vault":

            return (STATUS_CONFIGURED, str(vault_dir), "")

        return (STATUS_INVALID, "", "unknown")



    launcher = Launcher(

        paths={

            "staging": {"root": str(staging_dir)},

            "audit": {

                "root": str(audit_dir),

                "passed": str(audit_dir / "passed"),

                "failed": str(audit_dir / "failed"),

            },

        },

        picker=custom_picker,

        synthesis_factory=lambda confirm=None, config=None: VaultE2ESynthesis(),

        is_test_mode=True,

    )

    launcher._approval_reader = lambda: "y"



    # Select vault via application picker

    status, _, _ = launcher.pick_folder("vault")

    assert status == STATUS_CONFIGURED



    recorded_events: list[dict] = []

    orig_sink = launcher._sink

    def intercepting_sink(payload):

        recorded_events.append(payload)

        orig_sink(payload)

    launcher._sink = intercepting_sink



    before_snapshot = snapshot_vault(str(vault_dir))



    # Run novel query against the selected vault

    res = launcher.start_run(

        "Observation: Urban bicyclists frequently navigate dedicated green corridors swiftly.",

        source_mode="personal_vault",

        retrieval_mode="lexical",

    )

    run_id = res["run_id"] if isinstance(res, dict) else str(res)



    for _ in range(50):

        st = launcher.run_status(run_id)

        if st.get("accepted") is not None or st.get("active") is False:

            break

        time.sleep(0.1)



    st = launcher.run_status(run_id)

    assert st.get("accepted") is True

    assert st.get("executed") == 1



    # Verify telemetry confirmed reading authentic Vault notes

    retrieval_events = [

        ev for ev in recorded_events

        if ev.get("kind") == "status" and "evaluated against authentic Vault" in ev.get("message", "")

    ]

    assert len(retrieval_events) >= 1

    assert "vault unit(s)" in retrieval_events[0]["message"]



    # Verify Controlled Execution wrote to staging

    staged = list(staging_dir.glob("*.md"))

    assert len(staged) == 1



    # Verify strict read-only preservation

    after_snapshot = snapshot_vault(str(vault_dir))

    diff = diff_manifests(before_snapshot, after_snapshot)

    assert diff["stale"] is False

    assert diff["added"] == []

    assert diff["modified"] == []

    assert diff["removed"] == []





def test_vault_mode_resource_bounds_max_files(tmp_path):

    """Test 8 (Priority D): Vault exceeding 500 markdown notes respects _MAX_FILES = 500."""

    vault_dir = tmp_path / "large_file_count_vault"

    vault_dir.mkdir(parents=True, exist_ok=True)

    (vault_dir / "01 Core").mkdir(parents=True, exist_ok=True)

    (vault_dir / "01 Core" / "core-epistemology.md").write_text(

        "---\ntags: [core]\n---\nCore note epistemology.\n", encoding="utf-8"

    )



    # Create 520 notes total

    for i in range(520):

        (vault_dir / f"note_{i:04d}.md").write_text(

            f"---\ntags: [note]\n---\nEmpirical note content {i}.\n", encoding="utf-8"

        )



    files, skipped = discover_vault_files(str(vault_dir))

    assert len(files) == 500

    assert skipped == 21  # 521 notes total (520 + 1 core), 500 accepted, 21 skipped





def test_vault_mode_resource_bounds_oversized_file(tmp_path):

    """Test 9 (Priority D): Individual file exceeding 1 MB is excluded and counted in skipped."""

    vault_dir = tmp_path / "oversized_file_vault"

    vault_dir.mkdir(parents=True, exist_ok=True)



    normal_note = vault_dir / "normal_note.md"

    normal_note.write_text("Normal sized research note.\n", encoding="utf-8")



    # 1.2 MB note (exceeds _MAX_BYTES_PER_FILE = 1048576)

    large_note = vault_dir / "oversized_note.md"

    large_note.write_text("x" * (1024 * 1024 + 200 * 1024), encoding="utf-8")



    files, skipped = discover_vault_files(str(vault_dir))

    assert len(files) == 1

    assert files[0] == normal_note

    assert skipped == 1





def test_vault_mode_resource_bounds_total_bytes_limit(tmp_path):

    """Test 10 (Priority D): Vault notes exceeding 10 MB total stops processing deterministically."""

    vault_dir = tmp_path / "total_bytes_vault"

    vault_dir.mkdir(parents=True, exist_ok=True)



    # 15 files of 800 KB each = 12 MB total (exceeds _MAX_TOTAL_BYTES = 10 MB)

    chunk = "A" * (800 * 1024)

    for i in range(15):

        (vault_dir / f"batch_note_{i:02d}.md").write_text(chunk, encoding="utf-8")



    files, skipped = discover_vault_files(str(vault_dir))

    total_bytes = sum(f.stat().st_size for f in files)

    assert total_bytes <= 10 * 1024 * 1024

    assert len(files) == 12  # 12 * 800KB = 9.6MB, 13th would be 10.4MB > 10MB

    assert skipped == 3





def test_vault_mode_honest_status_telemetry_reporting(tmp_path):

    """Test 11 (Priority D): Pipeline run reports honest telemetry with exact skipped counts."""

    vault_dir = tmp_path / "telemetry_vault"

    vault_dir.mkdir(parents=True, exist_ok=True)

    (vault_dir / "01 Core").mkdir(parents=True, exist_ok=True)

    (vault_dir / "01 Core" / "core.md").write_text("Authoritative core.\n", encoding="utf-8")

    (vault_dir / "valid_note.md").write_text("Valid note.\n", encoding="utf-8")

    # Oversized note that gets skipped

    (vault_dir / "huge.md").write_text("Y" * (1024 * 1024 + 100), encoding="utf-8")



    staging_dir = tmp_path / "staging_zone"

    staging_dir.mkdir(parents=True, exist_ok=True)

    audit_dir = tmp_path / "audit_zone"

    audit_dir.mkdir(parents=True, exist_ok=True)

    AuditWriter(str(audit_dir)).ensure_directories()



    recorded_events: list[dict] = []

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

        synthesis_factory=lambda confirm=None, config=None: VaultE2ESynthesis(),

        is_test_mode=True,

    )

    launcher._approval_reader = lambda: "acknowledge"

    orig_sink = launcher._sink

    def intercepting_sink(payload):

        recorded_events.append(payload)

        orig_sink(payload)

    launcher._sink = intercepting_sink



    launcher._session_destinations["vault"] = str(vault_dir)

    launcher._session_destinations["staging"] = str(staging_dir)

    launcher._session_destinations["audit"] = str(audit_dir)



    res = launcher.start_run(

        "Observation: Valid empirical research statement demonstrating honest status telemetry with skipped vault notes.",

        source_mode="personal_vault",

        retrieval_mode="lexical",

    )

    run_id = res["run_id"] if isinstance(res, dict) else str(res)



    for _ in range(50):

        st = launcher.run_status(run_id)

        if st.get("accepted") is not None or st.get("active") is False:

            break

        time.sleep(0.1)



    # Verify telemetry reported skipped count honestly

    retrieval_status = [

        ev for ev in recorded_events

        if ev.get("kind") == "status" and "evaluated against authentic Vault" in ev.get("message", "")

    ]

    assert len(retrieval_status) >= 1

    msg = retrieval_status[0]["message"]

    assert "1 skipped" in msg
