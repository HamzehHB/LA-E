"""Input stage validation, provenance, safety, audit partitioning, and adapter integration tests.

Synthetic only: no real vault, no network, no staging or audit writes
outside pytest's temporary directories.
"""
import hashlib
import json
import time

import pytest

from src.living_authenticity.adapter import Launcher, LauncherServer
from src.living_authenticity.knowledge.governance.audit import (
    AuditWriter,
    audit_storage_category,
)
from src.living_authenticity.knowledge.input.processor import (
    ACCEPTED_SOURCE_TYPES,
    DEFAULT_MIN_LETTERS,
    MAX_TEXT_BYTES,
    InputProvenance,
    InputValidationResult,
    count_letters,
    count_tokens,
    process_input,
    record_input_failure_audit,
)
from tests.adapter.test_launcher_transport import _request, _server


def test_process_input_valid_text():
    """Valid text with >= 30 words and default chat_input returns valid result with provenance."""
    text = (
        "This is an authentic research observation documenting how existential autonomy and "
        "self-concordant decisions emerge across experiential modalities in qualitative inquiry, "
        "providing substantive textual evidence for authentic knowledge processing and validation."
    )
    result = process_input(text, source_type="chat_input", source_mode="personal_vault")

    assert result.valid is True
    assert result.reason == ""
    assert result.error_code == ""
    assert result.provenance is not None

    prov = result.provenance
    assert isinstance(prov, InputProvenance)
    assert prov.source_type == "chat_input"
    assert prov.resolved_type == "text"
    assert prov.operating_mode == "personal_vault"
    assert prov.byte_length == len(text.encode("utf-8"))
    assert prov.char_length == len(text)
    assert prov.input_hash == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert prov.input_id == f"inp-{prov.input_hash[:12]}"
    assert "T" in prov.timestamp


def test_process_input_persian_multiline_text():
    """Persian multiline input meeting word count computes accurate byte and character lengths."""
    text = (
        "یادداشت پژوهشی شماره یک درباره اصالت وجودی و خودآگاهی پایدار انسان.\n"
        "این سطر دوم است که مشاهدات تجربی، مبانی فلسفی و شواهد کیفی را با دقت فراوان "
        "برای ارزیابی و پردازش در بستر دانش مستندسازی و تحلیل می‌نماید تا محتوای کافی وجود داشته باشد."
    )
    result = process_input(text, source_type="chat_input", source_mode="examples")

    assert result.valid is True
    prov = result.provenance
    assert prov is not None
    assert prov.char_length == len(text)
    assert prov.byte_length == len(text.encode("utf-8"))
    assert prov.byte_length > prov.char_length  # UTF-8 multibyte Persian characters
    assert prov.input_id.startswith("inp-")


def test_process_input_custom_valid_input_id():
    """Preserves client-generated input_id if it follows the inp- prefix convention."""
    custom_id = "inp-client12345"
    text = (
        "This valid research note provides substantive phenomenological depth and clear conceptual "
        "boundaries across multiple qualitative dimensions to comfortably satisfy the minimum word count "
        "threshold required by the knowledge input evaluation system."
    )
    result = process_input(text, source_type="chat_input", input_id=custom_id)
    assert result.valid is True
    assert result.provenance is not None
    assert result.provenance.input_id == custom_id


def test_process_input_rejects_empty_and_whitespace():
    """Empty and whitespace-only text must be rejected with empty_input code."""
    res_empty = process_input("", source_type="chat_input")
    assert res_empty.valid is False
    assert res_empty.error_code == "empty_input"
    assert res_empty.provenance is None

    res_ws = process_input("   \t\n  \r\n  ", source_type="chat_input")
    assert res_ws.valid is False
    assert res_ws.error_code == "empty_input"
    assert res_ws.provenance is None


def test_process_input_rejects_non_string():
    """Non-string inputs must fail with invalid_input_type."""
    for invalid in [None, 12345, ["list"], {"key": "val"}]:
        res = process_input(invalid, source_type="chat_input")
        assert res.valid is False
        assert res.error_code == "invalid_input_type"
        assert res.provenance is None


def test_process_input_rejects_unsupported_source_type():
    """Only 'chat_input' is currently accepted; file uploads or voice are rejected."""
    for unsupported in ["file_upload", "voice", "scan", "api_batch", ""]:
        res = process_input("Some text", source_type=unsupported)
        assert res.valid is False
        assert res.error_code == "unsupported_source_type"
        assert "unsupported source_type" in res.reason


def test_process_input_rejects_invalid_source_mode():
    """Invalid source_mode must fail with invalid_source_mode."""
    for invalid_mode in ["unrestricted", "cloud", "production", ""]:
        res = process_input("Some text", source_type="chat_input", source_mode=invalid_mode)
        assert res.valid is False
        assert res.error_code == "invalid_source_mode"


def test_process_input_rejects_oversized_text():
    """Text exceeding max_bytes limit is rejected with size_limit_exceeded."""
    small_limit = 32
    oversized = "a" * (small_limit + 1)
    res = process_input(oversized, source_type="chat_input", max_bytes=small_limit)
    assert res.valid is False
    assert res.error_code == "size_limit_exceeded"
    assert "exceeds maximum limit" in res.reason


def test_record_input_failure_audit_partitions_into_failed_and_never_leaks_text(tmp_path):
    """Audit record for input validation failure partitions into failed/ and NEVER contains raw text."""
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    writer = AuditWriter(str(audit_root))

    secret_raw_text = "CONFIDENTIAL_PRIVATE_INPUT_RESEARCH_SECRET_XYZ"
    res = process_input(secret_raw_text, source_type="unsupported_source")
    assert res.valid is False

    receipt = record_input_failure_audit(
        writer,
        run_id="run-test-input-fail",
        validation_result=res,
        operating_mode="examples",
        input_text=secret_raw_text,
    )
    assert receipt is not None
    assert receipt["storage_category"] == "failed"
    assert "audit_path" in receipt

    # Verify audit file location: must be in failed/audit.jsonl
    failed_log = audit_root / "failed" / "audit.jsonl"
    assert failed_log.is_file(), "failure audit record must be written to failed/audit.jsonl"

    # Verify error log does NOT exist or does not contain this record (not classified as operational error)
    errors_log = audit_root / "errors" / "audit.jsonl"
    assert not errors_log.exists()

    content = failed_log.read_text(encoding="utf-8")
    records = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]

    assert rec["run_id"] == "run-test-input-fail"
    assert rec["terminal_outcome"] == "input_validation_failed"
    assert rec["storage_category"] == "failed"
    assert rec["operating_mode"] == "examples"
    assert rec["input_hash"] == hashlib.sha256(secret_raw_text.encode("utf-8")).hexdigest()
    assert rec["approval_approved"] is False
    assert rec["execution_attempted"] is False
    assert rec["execution_executed"] is False

    # CRITICAL PRIVACY VERIFICATION: Raw text must NEVER appear anywhere in the audit log file
    assert secret_raw_text not in content


def test_api_run_rejects_unsupported_source_type_synchronously():
    """POST /api/run with an unsupported source_type returns 400 validation_error immediately."""
    launcher, server = _server()
    try:
        status, data = _request(
            server,
            "POST",
            "/api/run",
            body={
                "input_text": "hello",
                "source_mode": "examples",
                "source_type": "file_upload",
            },
            token=launcher.token,
        )
        assert status == 400
        payload = json.loads(data)
        assert payload["error"]["kind"] == "validation_error"
        assert "unsupported source_type" in payload["error"]["message"]
    finally:
        server.shutdown()


def test_api_run_accepts_valid_chat_input():
    """POST /api/run with valid chat_input returns 202 with accepted=True and source_type."""
    launcher, server = _server()
    try:
        status, data = _request(
            server,
            "POST",
            "/api/run",
            body={
                "input_text": "A valid synthetic note for input stage testing.",
                "source_mode": "examples",
                "source_type": "chat_input",
                "input_id": "inp-test0001",
            },
            token=launcher.token,
        )
        assert status == 202
        payload = json.loads(data)
        assert payload["accepted"] is True
        assert payload["source_type"] == "chat_input"
        assert payload["run_id"].startswith("run-")
    finally:
        server.shutdown()


def test_run_thread_input_validation_failure_emits_events_and_audits(tmp_path):
    """When input validation fails inside _run_thread (e.g. whitespace-only), it halts and audits."""
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    launcher = Launcher(
        paths={"staging": {"root": ""}, "audit": {"root": str(audit_root)}},
        llm_config={"llm": {"provider": "disabled"}},
    )
    # Empty text bypasses /api/run if passed directly to start_run or _run_thread
    # but process_input catches it
    run_id = "run-empty-test"
    launcher._run_thread(
        run_id=run_id,
        input_text="   \n\t  ",
        mode_value="examples",
        source_type="chat_input",
    )

    events = launcher.events.since(0)
    kinds = [e.get("kind") for e in events]
    stages = [e.get("stage") for e in events]

    assert "run_started" in kinds
    assert "stage_started" in kinds
    assert "input" in stages
    assert "stopped" in kinds
    assert "run_rejected" in kinds

    # Verify run report
    report = launcher.run_status(run_id)
    assert report["accepted"] is False
    assert report["terminal_outcome"] == "input_validation_failed"
    assert report["processed"] == 0
    assert report["executed"] == 0

    # Verify audit record written to failed/audit.jsonl
    failed_log = audit_root / "failed" / "audit.jsonl"
    assert failed_log.is_file()
    records = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r.get("terminal_outcome") == "input_validation_failed" for r in records)


def test_run_thread_input_success_hands_off_to_ingestion_with_provenance(tmp_path):
    """Successful input validation emits stage_completed for input and passes provenance to ingestion."""
    staging_root = tmp_path / "staging"
    audit_root = tmp_path / "audit"
    staging_root.mkdir()
    audit_root.mkdir()

    launcher = Launcher(
        paths={"staging": {"root": str(staging_root)}, "audit": {"root": str(audit_root)}},
        llm_config={"llm": {"provider": "disabled"}},
    )

    input_text = (
        "Authentic researcher entry for downstream handoff test evaluating how ontological "
        "commitments and epistemological boundaries interact within qualitative research "
        "environments to yield actionable knowledge artifacts, verifiable observations, and robust "
        "empirical evidence across iterative validation phases."
    )
    run_id = "run-success-input"

    # Start run and wait briefly for completion
    resp = launcher.start_run(
        input_text=input_text,
        source_mode="examples",
        source_type="chat_input",
        input_id="inp-provtest123",
    )
    assert resp["accepted"] is True

    # Poll launcher report for up to 5 seconds
    deadline = time.time() + 5.0
    report = None
    while time.time() < deadline:
        report = launcher.run_status(resp["run_id"])
        if not report.get("active", False) and report.get("run_id"):
            break
        time.sleep(0.05)

    assert report is not None
    events = launcher.events.since(0)
    stage_events = [e for e in events if e.get("run_id") == resp["run_id"]]
    stages_completed = [e.get("stage") for e in stage_events if e.get("kind") == "stage_completed"]

    assert "input" in stages_completed

    # Check input stage completed event payload
    input_completed_ev = next(e for e in stage_events if e.get("kind") == "stage_completed" and e.get("stage") == "input")
    assert input_completed_ev["input_id"] == "inp-provtest123"
    assert "input_hash" in input_completed_ev

    # Check ingestion stage started event
    ingestion_started_ev = next((e for e in stage_events if e.get("kind") == "stage_started" and e.get("stage") == "ingestion"), None)
    assert ingestion_started_ev is not None


def test_load_knowledge_corpus_examples_mode():
    """In Test Mode ('examples'), corpus and core are strictly loaded from synthetic examples."""
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

    launcher = Launcher(
        paths={"obsidian": {"vault": ""}},
        llm_config={"llm": {"provider": "disabled"}},
    )
    pipe = EvidenceFirstPipeline()
    corpus_units, core_units, vault_ctx = launcher._load_knowledge_corpus("examples", pipe, launcher.paths(), run_id="run-test-ex")

    assert len(corpus_units) >= 2
    assert len(core_units) >= 1
    assert vault_ctx is None

    # Check status event emitted for retrieval
    events = launcher.events.since(0)
    retrieval_ev = next(e for e in events if e.get("stage") == "retrieval" and "Test Mode" in e.get("message", ""))
    assert "synthetic examples" in retrieval_ev["message"]


def test_load_knowledge_corpus_vault_mode_configured(tmp_path):
    """In Vault Mode ('personal_vault'), corpus is loaded from the researcher's authentic vault."""
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

    vault_dir = tmp_path / "MyAuthenticVault"
    vault_dir.mkdir()
    (vault_dir / "01-observation.md").write_text("# Authentic Observation\nDeep inquiry on authenticity and awareness.", encoding="utf-8")

    launcher = Launcher(
        paths={"obsidian": {"vault": str(vault_dir)}},
        llm_config={"llm": {"provider": "disabled"}},
    )
    pipe = EvidenceFirstPipeline()
    corpus_units, core_units, vault_ctx = launcher._load_knowledge_corpus("personal_vault", pipe, launcher.paths(), run_id="run-test-vault")

    assert len(corpus_units) == 2
    assert any("authenticity and awareness" in u.cleaned_text for u in corpus_units)
    assert vault_ctx is not None
    assert len(vault_ctx) == 1

    events = launcher.events.since(0)
    retrieval_ev = next(e for e in events if e.get("stage") == "retrieval" and "Vault Mode" in e.get("message", ""))
    assert "authentic Vault" in retrieval_ev["message"]


def test_load_knowledge_corpus_vault_mode_unconfigured():
    """When vault is not configured, Vault Mode reports honest status and returns empty baseline."""
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

    launcher = Launcher(
        paths={"obsidian": {"vault": ""}},
        llm_config={"llm": {"provider": "disabled"}},
    )
    pipe = EvidenceFirstPipeline()
    corpus_units, core_units, vault_ctx = launcher._load_knowledge_corpus("personal_vault", pipe, launcher.paths(), run_id="run-test-unconf")

    assert corpus_units == []
    assert core_units == []
    assert vault_ctx is None

    events = launcher.events.since(0)
    retrieval_ev = next(e for e in events if e.get("stage") == "retrieval" and "Vault Mode" in e.get("message", ""))
    assert "not configured" in retrieval_ev["message"]


def test_count_letters_and_tokens_accuracy():
    """Letters and tokens are accurately identified across English, Persian (with ZWNJ), and symbols."""
    # Persian with ZWNJ
    fa_text = "این پژوهش‌ها پیوسته بررسی می‌شوند."
    assert count_letters(fa_text) == 27
    assert count_tokens(fa_text) == 5

    # English with punctuation and numbers
    en_text = "Authentic note #42! Validated at 100%."
    assert count_letters(en_text) == 24  # A-u-t-h-e-n-t-i-c (9) + n-o-t-e (4) + V-a-l-i-d-a-t-e-d (9) + a-t (2)
    assert count_tokens(en_text) == 6   # Authentic, note, 42, Validated, at, 100

    # Punctuation only
    assert count_letters("??? ... --- !!!") == 0
    assert count_tokens("??? ... --- !!!") == 0


def test_process_input_halts_under_min_letters_boundary():
    """Inputs with fewer than 50 letters halt at input stage with NEEDS_CLARIFICATION and Persian reason."""
    short_inputs = [
        "???",
        "....",
        "---",
        "aaaaaaaaaaaaaaaa",
        "asdfasdfasdfasdf",
        "a",
        "x",
        "1",
        "ب",
        "Why?",
        "چرا؟",
        "AI-001",
        "x = y + 1",
        "TODO: verify citation [42]",
        "اصالت وجودی و خودآگاهی انسان",
        "Is authenticity relational or ontological?",
        "This short note has only thirty-five letters total.",
    ]
    for short_text in short_inputs:
        res = process_input(short_text)
        assert res.valid is False
        assert res.outcome.value == "NEEDS_CLARIFICATION"
        assert res.error_code == "insufficient_letter_count"
        assert "نیاز به داده بیشتر برای سنجش است" in res.reason
        assert res.recovery_options == ()  # No Fix or Stop for INPUT


def test_process_input_rejects_repeated_character_spam():
    """Inputs with repetitive identical character sequences exceeding letter limit are rejected as spam."""
    spam = "aaaaaaaaaaaaaaaa " * 35
    res = process_input(spam)
    assert res.valid is False
    assert res.outcome.value == "REJECTED"
    assert res.error_code == "repeated_char_noise"
    assert res.recovery_options == ()  # No Fix or Stop for INPUT


def test_process_input_accepts_substantive_multilingual_research():
    """Research content meeting the 30-word minimum boundary is accepted with provenance."""
    text_en = (
        "This qualitative research inquiry investigates how authentic individual agency and "
        "existential self-awareness interact within organizational knowledge structures, comparing "
        "traditional ontological assumptions with contemporary phenomenology across longitudinal field "
        "observations and iterative hermeneutic analysis."
    )
    res_en = process_input(text_en, source_type="chat_input", source_mode="examples")
    assert res_en.valid is True
    assert res_en.outcome.value == "ACCEPTED"
    assert res_en.can_proceed is True
    assert res_en.provenance is not None

    text_fa = (
        "این پژوهش کیفی و اصیل به بررسی چگونگی شکل‌گیری آگاهی انسانی و پیوند آن با تصمیم‌گیری "
        "مستقل در بستر محیط‌های چندگانه اجتماعی و فلسفی می‌پردازد تا داده‌ها و شواهد تجربی کافی "
        "برای پردازش عمیق و بازیابی دقیق مفاهیم بنیادین فراهم گردد."
    )
    res_fa = process_input(text_fa, source_type="chat_input", source_mode="personal_vault")
    assert res_fa.valid is True
    assert res_fa.outcome.value == "ACCEPTED"
    assert res_fa.can_proceed is True
    assert res_fa.provenance is not None


def test_clarification_outcome_does_not_proceed_to_ingestion(tmp_path):
    """When input needs clarification, pipeline stops at input stage and does not start ingestion."""
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    launcher = Launcher(
        paths={"audit": {"root": str(audit_root)}},
        llm_config={"llm": {"provider": "disabled"}},
    )
    run_id = "run-clarify-test"
    launcher._run_thread(
        run_id=run_id,
        input_text="x",  # Ambiguous single token
        mode_value="examples",
    )

    events = launcher.events.since(0)
    kinds = [e.get("kind") for e in events]
    stages = [e.get("stage") for e in events]

    assert "run_rejected" in kinds
    # Ingestion stage must NOT have started
    assert not any(e.get("stage") == "ingestion" for e in events)

    report = launcher.run_status(run_id)
    assert report["accepted"] is False
    assert report["outcome"] == "NEEDS_CLARIFICATION"
    assert report["terminal_outcome"] == "input_clarification_required"


def test_cancellation_before_downstream_stops_cleanly(tmp_path):
    """When a run is cancelled cooperatively, it stops at the safe boundary and audits."""
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    launcher = Launcher(
        paths={"audit": {"root": str(audit_root)}},
        llm_config={"llm": {"provider": "disabled"}},
    )
    run_id = "run-cancel-early"
    secret_text = "A completely valid private note that gets cancelled before processing."
    # Mark run_id as cancelled prior to or during execution
    with launcher._lock:
        launcher._cancel_flags.add(run_id)

    launcher._run_thread(
        run_id=run_id,
        input_text=secret_text,
        mode_value="examples",
    )

    events = launcher.events.since(0)
    stop_events = [e for e in events if e.get("kind") == "stopped"]
    assert len(stop_events) >= 1
    assert any("cancelled" in e.get("message", "").lower() for e in stop_events)

    report = launcher.run_status(run_id)
    assert report["accepted"] is False
    assert report["outcome"] == "STOPPED"
    assert report["terminal_outcome"] == "stopped_by_user"
    # Ingestion must not have executed
    assert not any(e.get("stage") == "ingestion" for e in events)

    # Audit verification: Cancellation must be recorded in failed/audit.jsonl
    failed_log = audit_root / "failed" / "audit.jsonl"
    assert failed_log.is_file(), "cancellation must be persisted to failed/audit.jsonl"
    records = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert any(r.get("terminal_outcome") == "stopped_by_user" for r in records)

    # Privacy verification: raw text must NEVER be leaked to audit
    content = failed_log.read_text(encoding="utf-8")
    assert secret_text not in content


