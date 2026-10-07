"""Operating modes, guarded audit root, vault sync, audit persistence,
and model discovery contracts.
"""
import json

import pytest

from src.living_authenticity.knowledge.modes import (
    normalize_mode,
    resolve_vault_for_mode,
)
from src.living_authenticity.knowledge.integration.guard import (
    check_audit_eligible,
    check_roots_disjoint,
    resolve_authorized_audit_root,
)
from src.living_authenticity.knowledge.vault.context import (
    diff_manifests,
    snapshot_vault,
    verify_index_freshness,
)
from src.living_authenticity.knowledge.governance.audit.outcome import (
    AuditRecord,
)
from src.living_authenticity.knowledge.governance.audit.persistence import (
    AuditWriter,
    audit_storage_category,
    record_to_dict,
)
from src.living_authenticity.llm.discovery import (
    describe_selection,
    discover_local_models,
)


def test_examples_mode_never_resolves_configured_vault():
    with pytest.raises(ValueError):
        resolve_vault_for_mode("examples",
                               configured_vault="/some/personal/vault")
    assert resolve_vault_for_mode("examples") == ""
    assert normalize_mode("personal_vault") == "personal_vault"


def test_personal_mode_prefers_explicit_vault():
    assert resolve_vault_for_mode("personal_vault",
                                  configured_vault="/cfg",
                                  explicit_vault="/picked") == "/picked"
    assert resolve_vault_for_mode("personal_vault",
                                  configured_vault="/cfg") == "/cfg"


def test_audit_root_resolves_from_config_and_rejects_overlap(tmp_path):
    audit = tmp_path / "audit"
    staging = tmp_path / "staging"
    vault = tmp_path / "vault"
    for directory in (audit, staging, vault):
        directory.mkdir()
    config = {"audit": {"root": str(audit)}}
    assert resolve_authorized_audit_root(config) == str(audit)
    eligible, _reason, _check = check_audit_eligible(
        str(audit), guarded_roots=(str(vault), str(staging), str(audit)),
        authorized_audit_root=str(audit))
    assert eligible is True
    disjoint, _reason, _check = check_roots_disjoint(
        str(staging), str(audit), str(tmp_path / "index"), str(vault))
    assert disjoint is True
    overlapped, _reason, _check = check_roots_disjoint(
        str(staging), str(staging), "", "")
    assert overlapped is False


def test_vault_manifest_diff_and_freshness(tmp_path):
    first = tmp_path / "a.md"
    first.write_text("alpha", encoding="utf-8")
    before = snapshot_vault(str(tmp_path))
    first.write_text("alpha changed", encoding="utf-8")
    (tmp_path / "b.md").write_text("beta", encoding="utf-8")
    after = snapshot_vault(str(tmp_path))
    diff = diff_manifests(before, after)
    assert diff["stale"] is True
    assert "b.md" in diff["added"]
    assert len(diff["modified"]) == 1
    freshness = verify_index_freshness(after, {})
    assert freshness["fresh"] is False
    assert freshness["missing"]


def test_audit_writer_partitions_passed_and_failed(tmp_path):
    root = tmp_path / "audit-root"
    passed = root / "passed"
    failed = root / "failed"
    writer = AuditWriter(str(root), str(passed), str(failed))
    executed = AuditRecord(record_id="ok", run_id="run1",
                           proposal_hash="h", query_unit_id="q",
                           execution_executed=True)
    held = AuditRecord(record_id="no", run_id="run1",
                       proposal_hash="h2", query_unit_id="q2",
                       approval_approved=False)
    # An approved + revalidated pre-execution checkpoint is a safety
    # prerequisite, NOT a passed terminal outcome.
    pre_execution = AuditRecord(
        record_id="pre", run_id="run1", proposal_hash="h3",
        query_unit_id="q3", approval_approved=True,
        revalidation_performed=True, revalidation_valid=True,
        audit_phase="pre_execution")
    writer.append(executed)
    writer.append(held)
    writer.append(pre_execution)
    passed_lines = (passed / "audit.jsonl").read_text(
        encoding="utf-8").splitlines()
    failed_lines = (failed / "audit.jsonl").read_text(
        encoding="utf-8").splitlines()
    assert len(passed_lines) == 1
    assert len(failed_lines) == 2
    assert json.loads(passed_lines[0])["storage_category"] == "passed"
    for line in failed_lines:
        assert json.loads(line)["storage_category"] == "failed"
    assert audit_storage_category(executed) == "passed"
    assert audit_storage_category(held) == "failed"
    assert audit_storage_category(pre_execution) == "failed"


def test_audit_writer_appends_and_never_stores_credentials(tmp_path):
    root = tmp_path / "audit-root"
    writer = AuditWriter(str(root))
    record = AuditRecord(record_id="r1", run_id="run1",
                         proposal_hash="h", query_unit_id="q",
                         execution_executed=True)
    receipt = writer.append(record)
    assert receipt["record_digest"]
    assert receipt["storage_category"] == "passed"
    log = root / "passed" / "audit.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["record_id"] == "r1"
    assert payload["timestamp"]
    assert payload["run_id"] == "run1"
    from src.living_authenticity.knowledge.governance.audit.persistence import (
        _sanitize,
    )
    cleaned = _sanitize({"meta": {"api_key": "secret-value", "note": "ok"},
                         "items": [{"password": "p"}, "plain"]})
    blob = json.dumps(cleaned).lower()
    assert "secret-value" not in blob
    assert "api_key" not in blob
    assert "password" not in blob
    assert record_to_dict(record)["record_id"] == "r1"
    writer.append(record)
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_audit_writer_rejects_traversal_subroots(tmp_path):
    root = tmp_path / "audit-root"
    root.mkdir()
    escape = tmp_path / "escape"
    escape.mkdir()
    with pytest.raises(ValueError):
        AuditWriter(str(root), passed_dir=str(escape),
                    failed_dir=str(root / "failed"))
    with pytest.raises(ValueError):
        AuditWriter(str(root),
                    passed_dir=str(root / "passed" / ".." / ".." / "escape"),
                    failed_dir=str(root / "failed"))


def test_audit_subroots_must_resolve_inside_root(tmp_path):
    audit = tmp_path / "audit"
    audit.mkdir()
    passed = audit / "passed"
    passed.mkdir()
    bad = tmp_path / "outside"
    bad.mkdir()
    ok, _reason, _check = check_audit_eligible(
        str(audit), guarded_roots=(str(audit),),
        authorized_audit_root=str(audit),
        authorized_passed=str(passed),
        authorized_failed=str(bad))
    assert ok is False
    ok2, _reason2, _check2 = check_audit_eligible(
        str(audit), guarded_roots=(str(audit),),
        authorized_audit_root=str(audit),
        authorized_passed=str(passed),
        authorized_failed=str(audit / "failed"))
    assert ok2 is True


def test_vault_manifest_detects_removed_and_moved(tmp_path):
    first = tmp_path / "a.md"
    first.write_text("alpha", encoding="utf-8")
    second = tmp_path / "b.md"
    second.write_text("beta", encoding="utf-8")
    before = snapshot_vault(str(tmp_path))
    second.unlink()
    (tmp_path / "c.md").write_text("beta", encoding="utf-8")
    after = snapshot_vault(str(tmp_path))
    diff = diff_manifests(before, after)
    assert "b.md" in diff["removed"]
    assert "c.md" in diff["added"]
    assert diff["stale"] is True
    # Same content under a new name is represented as remove+add
    # (stale), not an automatic rename rewrite of the index.


def test_local_model_discovery_is_read_only(tmp_path):
    (tmp_path / "model.gguf").write_bytes(b"fake")
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    result = discover_local_models(str(tmp_path))
    assert [item["filename"] for item in result["models"]] == ["model.gguf"]
    assert (tmp_path / "model.gguf").exists()
    selection = describe_selection({"llm": {"provider": "ollama",
                                             "model": "m",
                                             "endpoint": "http://127.0.0.1:11434"}})
    assert selection["valid"] is True
    assert selection["category"] == "local"
    cloud = describe_selection({"llm": {"provider": "cloud",
                                        "model": "m",
                                        "endpoint": "https://example.com"}})
    assert cloud["category"] == "cloud"
