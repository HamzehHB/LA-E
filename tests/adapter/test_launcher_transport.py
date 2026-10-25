"""Loopback launcher transport: auth, validation, and boundary tests.

Synthetic only: no real vault, no network beyond 127.0.0.1, no staging
or audit writes outside pytest's temporary directories.
"""
import http.client
import json

import pytest

from src.living_authenticity.adapter import Launcher, LauncherServer

TOKEN_HEADER = "X-LAE-Token"


def _launcher(**kwargs):
    kwargs.setdefault(
        "paths", {"staging": {"root": ""}, "audit": {"root": ""}})
    kwargs.setdefault("llm_config", {"llm": {"provider": "disabled"}})
    return Launcher(**kwargs)


def _server(**kwargs):
    launcher = _launcher(**kwargs)
    server = LauncherServer(launcher)
    server.serve_in_thread()
    return launcher, server


def _request(server, method, path, body=None, token=None,
             headers=None, origin=None, raw_length=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    hdrs = dict(headers or {})
    hdrs.setdefault("Connection", "close")
    if token is not None:
        hdrs[TOKEN_HEADER] = token
    if origin is not None:
        hdrs["Origin"] = origin
    if raw_length is not None:
        conn.putrequest(method, path)
        for name, value in hdrs.items():
            conn.putheader(name, value)
        conn.putheader("Content-Length", str(raw_length))
        conn.endheaders()
        resp = conn.getresponse()
        data = resp.read().decode("utf-8")
        conn.close()
        return resp.status, data
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp.status, data


@pytest.fixture()
def running():
    launcher, server = _server()
    yield launcher, server
    server.shutdown()

def test_health_is_unauthenticated_and_minimal(running):
    _launcher, server = running
    status, data = _request(server, "GET", "/api/health")
    assert status == 200
    payload = json.loads(data)
    assert payload["app"] == "living-authenticity"
    assert payload["instance_id"]
    assert "token" not in payload


def test_unknown_api_route_requires_token(running):
    _launcher, server = running
    status, _ = _request(server, "GET", "/api/journals")
    assert status == 401


def test_invalid_token_is_rejected(running):
    _launcher, server = running
    status, data = _request(
        server, "GET", "/api/journals", token="wrong-token")
    assert status == 403
    assert json.loads(data)["error"]["kind"] == "authentication_error"


def test_valid_token_accepts_journals(running):
    launcher, server = running
    status, data = _request(
        server, "GET", "/api/journals", token=launcher.token)
    assert status == 200
    payload = json.loads(data)
    assert payload["audits"] == []
    assert payload["staging"] == []


def test_non_json_content_type_rejected(running):
    launcher, server = running
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    conn.request("POST", "/api/run", body=b"{}", headers={
        "Content-Type": "text/plain",
        TOKEN_HEADER: launcher.token,
    })
    resp = conn.getresponse()
    conn.close()
    assert resp.status == 415


def test_oversized_body_rejected_before_reading(running):
    launcher, server = running
    status, _ = _request(server, "POST", "/api/run",
                         token=launcher.token, raw_length=5_000_000)
    assert status == 413


def test_malformed_json_rejected(running):
    launcher, server = running
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    conn.request("POST", "/api/run", body=b"{not json",
                 headers={"Content-Type": "application/json",
                          TOKEN_HEADER: launcher.token})
    resp = conn.getresponse()
    conn.close()
    assert resp.status == 400


def test_non_object_json_rejected(running):
    launcher, server = running
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
    conn.request("POST", "/api/run", body=b"[1,2,3]",
                 headers={"Content-Type": "application/json",
                          TOKEN_HEADER: launcher.token})
    resp = conn.getresponse()
    conn.close()
    assert resp.status == 400


def test_unknown_route_returns_404_with_token(running):
    launcher, server = running
    status, _ = _request(server, "POST", "/api/nothing", body={},
                         token=launcher.token)
    assert status == 404


def test_cross_origin_request_rejected(running):
    launcher, server = running
    status, _ = _request(server, "GET", "/api/health",
                         origin="http://evil.example")
    assert status == 403


def test_non_loopback_host_header_rejected(running):
    launcher, server = running
    status, _ = _request(server, "GET", "/api/health",
                         headers={"Host": "evil.example"})
    assert status == 403


def test_folder_pick_rejects_unknown_purpose(running):
    launcher, server = running
    status, data = _request(server, "POST", "/api/folders/pick",
                            body={"purpose": "/etc"},
                            token=launcher.token)
    assert status == 422


def test_folder_pick_ignores_frontend_supplied_paths(running):
    launcher, server = running
    seen = []

    def _picker(purpose):
        seen.append(purpose)
        return ("cancelled", "", "cancelled")

    launcher._picker = _picker
    status, data = _request(
        server, "POST", "/api/folders/pick",
        body={"purpose": "vault", "path": "/some/absolute/path"},
        token=launcher.token)
    assert status == 200
    assert seen == ["vault"]
    payload = json.loads(data)
    assert payload["status"] == "cancelled"


def test_journals_route_requires_token(running):
    _launcher, server = running
    status, _ = _request(server, "GET", "/api/journals")
    assert status == 401


def test_journals_route_returns_correlated_shape(running):
    launcher, server = running
    status, data = _request(
        server, "GET", "/api/journals", token=launcher.token)
    assert status == 200
    payload = json.loads(data)
    assert set(payload.keys()) == {"audits", "staging"}
    assert isinstance(payload["audits"], list)
    assert isinstance(payload["staging"], list)


def test_folder_pick_validated_at_is_timestamp(running):
    launcher, server = running
    seen = []

    def _picker(purpose):
        seen.append(purpose)
        return ("cancelled", "", "cancelled")

    launcher._picker = _picker
    status, data = _request(
        server, "POST", "/api/folders/pick",
        body={"purpose": "vault"}, token=launcher.token)
    assert status == 200
    payload = json.loads(data)
    assert payload["validated_at"] != "now"
    assert "T" in payload["validated_at"]
    assert payload["display"] == ""


def test_session_destination_flows_from_pick_to_run(tmp_path):
    """A UI-validated staging pick is used by the next run.

    Portable path selection: pick_folder() validates through the
    existing guards, remembers the validated choice for this launch
    only, and _execute_run() prefers it over the config file value.
    Guards still revalidate at execution time (fail closed).
    """
    from src.living_authenticity.security.path_boundary import PathBoundary

    staging = tmp_path / "ui-staging"
    staging.mkdir()
    outside = str(staging)
    from Config.settings import ROOT as REPOSITORY_ROOT

    assert not PathBoundary(str(REPOSITORY_ROOT)).is_allowed(outside)
    launcher = _launcher(paths={"staging": {"root": ""},
                                "audit": {"root": ""}},
                         picker=lambda purpose: ("configured", outside, ""))
    status, chosen, _reason = launcher.pick_folder("staging")
    assert status == "configured"
    assert launcher.session_destination("staging") == outside
    seen = {}

    def _fake_run(source_root, staging_arg, **kwargs):
        seen["staging_arg"] = staging_arg
        seen["audit_arg"] = kwargs.get("audit_root")

        class _Report:
            accepted = True
            rejection_reason = ""
            files_processed = 0
            units = ()

        return _Report()

    import src.living_authenticity.knowledge.integration as integration

    real = integration.run_bounded_integration
    integration.run_bounded_integration = _fake_run
    try:
        from src.living_authenticity.knowledge.orchestration import (
            EvidenceFirstPipeline,
        )
        launcher._pipeline_factory = lambda sink: EvidenceFirstPipeline(
            stage_observer=sink)
        launcher._execute_run("run-test-001",
                              "synthetic portable probe text",
                              "examples")
    finally:
        integration.run_bounded_integration = real
    assert seen["staging_arg"] == outside


def test_unvalidated_pick_is_never_remembered(tmp_path):
    """Cancelled/invalid picks leave no session destination behind."""
    launcher = _launcher(picker=lambda purpose: ("cancelled", "", ""))
    status, _chosen, _reason = launcher.pick_folder("staging")
    assert status == "cancelled"
    assert launcher.session_destination("staging") == ""



def test_proposal_edit_endpoint_contract():
    """POST /api/proposal/edit: valid edit, invalid edit, no pending."""
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.output.evidence_note import (
        DefaultOutputGenerator,
    )
    from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
        ExplicitApprovalGate,
    )
    import dataclasses

    launcher, server = _server()
    # No pending approval -> 400.
    status, _data = _request(
        server, "POST", "/api/proposal/edit",
        body={"title": "T", "body": "B"}, token=launcher.token)
    assert status == 400

    # Register a real pending CREATE approval with authoritative objects.
    query = KnowledgeUnit(id="u1", source="src", original_text="hello",
                          meaning="hello", position=0)
    proposal = Proposal(query_unit_id="u1", query_source="src",
                        query_position=0, action="CREATE", title="T0",
                        destination="Inbox", reason="r",
                        provenance=("query:u1@src#0",))
    note = DefaultOutputGenerator().generate(query, proposal=proposal)
    note = dataclasses.replace(note, markdown="Body zero.", body="Body zero.")
    gate = ExplicitApprovalGate()
    request = gate.build_request(query, proposal, None, note)
    launcher.register_approval(request, note, proposal=proposal)

    # Valid edit: new authoritative hash, supersedes the old one.
    status2, data2 = _request(
        server, "POST", "/api/proposal/edit",
        body={"title": "T1", "body": "Body one.",
              "proposal_hash": "attacker-claimed-hash"},
        token=launcher.token)
    assert status2 == 200
    payload = json.loads(data2)
    assert payload["proposal_hash"] != "attacker-claimed-hash"
    assert payload["supersedes"] == request.proposal_hash
    assert payload["title"] == "T1"
    assert payload["body"] == "Body one."
    assert payload["review_state"] == "awaiting_reapproval"

    # Invalid edit (empty body) is a 400 validation error.
    status3, data3 = _request(
        server, "POST", "/api/proposal/edit",
        body={"title": "T2", "body": "   "}, token=launcher.token)
    assert status3 == 400
    assert json.loads(data3)["error"]["kind"] == "validation_error"


def test_start_run_validates_retrieval_mode_and_returns_it():
    """retrieval_mode is validated and echoed; invalid is rejected 400."""
    launcher, server = _server()
    status, data = _request(
        server, "POST", "/api/run",
        body={"input_text": "synthetic probe", "source_mode": "examples",
              "retrieval_mode": "semantic"},
        token=launcher.token)
    assert status == 202
    payload = json.loads(data)
    assert payload["retrieval_mode"] == "semantic"
    assert payload["accepted"] is True
    status2, data2 = _request(
        server, "POST", "/api/run",
        body={"input_text": "x", "source_mode": "examples",
              "retrieval_mode": "bogus"},
        token=launcher.token)
    assert status2 == 400
    assert json.loads(data2)["error"]["kind"] == "validation_error"


def test_cancel_request_and_unknown_run():
    """Cancel an unknown/inactive run is a 400; valid token required."""
    launcher, server = _server()
    # No token.
    status, _ = _request(server, "POST", "/api/cancel",
                         body={"run_id": "run-nope"})
    assert status == 401
    # Token, but no active run with that id.
    status2, data2 = _request(server, "POST", "/api/cancel",
                              body={"run_id": "run-nope"},
                              token=launcher.token)
    assert status2 == 400
    assert json.loads(data2)["error"]["kind"] == "validation_error"


def test_cancelled_run_does_not_execute(tmp_path):
    """A cancelled run records 'cancelled' and writes nothing to staging.

    Drives the real per-candidate governance path (_govern_candidate) with a
    CREATE proposal, an auto-approving reader, and an armed cancellation
    token, proving the pre-execution boundary prevents the controlled write
    and routes an operational 'cancelled' audit to errors/.
    """
    from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
        ExplicitApprovalGate,
    )
    from src.living_authenticity.knowledge.governance.execution import (
        ControlledExecutor,
    )
    from src.living_authenticity.knowledge.governance.revalidation import (
        Revalidator,
    )
    from src.living_authenticity.knowledge.governance.audit import (
        AuditWriter,
        audit_storage_category,
    )
    from src.living_authenticity.knowledge.integration.unit_loop import (
        _govern_candidate,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )
    from src.living_authenticity.knowledge.output.evidence_note import (
        DefaultOutputGenerator,
    )
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    import dataclasses
    import json

    staging = tmp_path / "staging"
    audit_root = tmp_path / "audit"
    staging.mkdir()
    audit_root.mkdir()

    query = KnowledgeUnit(id="u1", source="src", original_text="hello",
                          meaning="hello", position=0)
    proposal = Proposal(query_unit_id="u1", query_source="src",
                        query_position=0, action="CREATE", title="T",
                        destination="Inbox", reason="r",
                        provenance=("query:u1@src#0",))
    note = DefaultOutputGenerator().generate(query, proposal=proposal)
    note = dataclasses.replace(note, markdown="Body text.", body="Body text.")

    entries: list = []
    audits: list = []
    _govern_candidate(
        "child.md", query, proposal, None, note,
        ExplicitApprovalGate(), Revalidator(), ControlledExecutor(),
        str(staging), (), "", lambda: "y", entries, audits,
        authorized_staging="",
        audit_writer=AuditWriter(str(audit_root)),
        should_cancel=lambda: True,
    )

    # Nothing was written to staging: cancellation stopped execution.
    assert list(staging.iterdir()) == [], "a cancelled run must not execute"
    # The candidate recorded a 'cancelled' terminal outcome.
    assert entries and entries[-1].outcome == "cancelled", [
        getattr(e, "outcome", "") for e in entries]
    # The cancelled audit record is classified as an operational error
    # (errors/), and persisting it lands it in the errors/ partition.
    assert audits, "a cancelled candidate must produce an audit record"
    assert audit_storage_category(audits[-1]) == "errors"
    writer = AuditWriter(str(audit_root))
    for record in audits:
        writer.append(record)
    errors_log = audit_root / "errors" / "audit.jsonl"
    assert errors_log.is_file(), "cancellation must be recorded in errors/"
    records = [json.loads(l) for l in
               errors_log.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(r.get("storage_category") == "errors" for r in records)


def test_run_scoped_events_attribution():
    """Run-scoped kinds inherit the active run_id; unknown kinds never do.

    Prevents SSE attribution of events that do not describe the current
    run (e.g. future/global kinds) to whatever run is active.
    """
    launcher = _launcher()
    launcher._active_run_id = "run-active-009"
    launcher._sink({"kind": "stage_completed", "stage": "retrieval",
                    "message": "ok"})
    staged = launcher.events.since(0)[-1]
    assert staged["run_id"] == "run-active-009"
    launcher._sink({"kind": "future_global_kind", "message": "not a run"})
    other = launcher.events.since(0)[-1]
    assert other.get("run_id", "") == ""
    launcher._sink({"kind": "status", "run_id": "run-other-001",
                    "message": "kept"})
    kept = launcher.events.since(0)[-1]
    assert kept["run_id"] == "run-other-001"


def test_start_run_validates_language_fields():
    """Language fields are validated at the boundary and echoed; app
    language is kept separate from the content languages. Invalid values
    are rejected rather than silently ignored."""
    launcher, server = _server()
    status, data = _request(
        server, "POST", "/api/run",
        body={"input_text": "synthetic probe", "source_mode": "examples",
              "staging_language": "fa", "audit_language": "en",
              "app_language": "fa"},
        token=launcher.token)
    assert status == 202
    payload = json.loads(data)
    assert payload["staging_language"] == "fa"
    assert payload["audit_language"] == "en"
    # app_language is returned separately and is not a content language.
    assert payload["app_language"] == "fa"
    # Unsupported value is rejected, never silently accepted.
    status2, data2 = _request(
        server, "POST", "/api/run",
        body={"input_text": "x", "source_mode": "examples",
              "staging_language": "de"},
        token=launcher.token)
    assert status2 == 400
    assert json.loads(data2)["error"]["kind"] == "validation_error"


def test_execute_run_records_requested_languages_without_translation(tmp_path):
    """_execute_run records the requested content languages on the run but
    does NOT translate generated content (no translation provider exists);
    the real content pipeline still runs unchanged."""
    import src.living_authenticity.knowledge.integration as integration

    launcher = _launcher(paths={"staging": {"root": ""}, "audit": {"root": ""}})
    called = {}

    def _fake_run(source_root, staging_arg, **kwargs):
        called["kwargs"] = kwargs

        class _Report:
            accepted = True
            rejection_reason = ""
            files_processed = 0
            units = ()

        return _Report()

    real = integration.run_bounded_integration
    integration.run_bounded_integration = _fake_run
    try:
        from src.living_authenticity.knowledge.orchestration import (
            EvidenceFirstPipeline,
        )
        launcher._pipeline_factory = lambda sink: EvidenceFirstPipeline(
            stage_observer=sink)
        launcher._execute_run("run-lang-001", "synthetic probe text",
                              "examples", staging_language="fa",
                              audit_language="en")
    finally:
        integration.run_bounded_integration = real

    # The real content pipeline was still invoked (nothing short-circuited).
    assert called.get("kwargs") is not None
    # An honest localization status event was recorded on the run.
    loc = [e for e in launcher.events.since(0)
           if e.get("stage") == "localization"]
    assert loc, "requested content languages must be recorded on the run"
    msg = loc[-1]["message"]
    assert "staging=fa" in msg and "audit=en" in msg
    assert "no translation provider" in msg


def test_post_config_updates_localization_and_loads(tmp_path, monkeypatch):
    """POST /api/config persists language schema correctly and updates settings."""
    import Config.settings as settings

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    example = config_dir / "localization.example.yaml"
    example.write_text(
        "language:\n  app: en\n  staging: en\n  audit: en\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(settings, "LOCALIZATION_EXAMPLE_FILE", "localization.example.yaml")
    monkeypatch.setattr(settings, "LOCALIZATION_LOCAL_FILE", "localization.local.yaml")

    launcher, server = _server()
    try:
        status, data = _request(
            server, "POST", "/api/config",
            body={"app_language": "fa", "staging_language": "fa", "audit_language": "en"},
            token=launcher.token,
        )
        assert status == 200
        res = json.loads(data)
        assert res["success"] is True
        assert res["modes"]["app_language"] == "fa"
        assert res["modes"]["staging_language"] == "fa"
        assert res["modes"]["audit_language"] == "en"

        # Verify that settings.load_languages() successfully loads the persisted updates
        loaded = settings.load_languages(config_dir)
        assert loaded["language"]["app"] == "fa"
        assert loaded["language"]["staging"] == "fa"
        assert loaded["language"]["audit"] == "en"
    finally:
        server.shutdown()


def test_run_thread_handles_terminal_audit_persistence_failure():
    """When staging succeeds but terminal audit fails, audit_persisted is False and warning is emitted."""
    from src.living_authenticity.knowledge.integration.outcome import IntegrationRunReport

    launcher = _launcher()
    failed_receipt = {
        "record_id": "rec-01",
        "persisted": False,
        "storage_path": "",
        "reason": "Disk full during audit write",
    }
    fake_report = IntegrationRunReport(
        accepted=True,
        files_seen=1,
        files_processed=1,
        audit_receipts=(failed_receipt,),
    )

    summary = launcher._report_summary(fake_report)
    assert summary["audit_persisted"] is False
    assert "Disk full" in summary["audit_warning"]

    # Verify _run_thread emits warning event
    import src.living_authenticity.knowledge.integration as integration
    old_run = integration.run_bounded_integration
    integration.run_bounded_integration = lambda *a, **kw: fake_report
    try:
        valid_research_text = (
            "Authentic qualitative research observation detailing experiential self-awareness and "
            "ontological congruence across empirical inquiry modalities with sufficient textual depth "
            "and conceptual rigor to comfortably satisfy minimum input requirements for testing downstream "
            "terminal integration and audit persistence capabilities."
        )
        launcher._run_thread("run-partial-01", valid_research_text, "examples")
    finally:
        integration.run_bounded_integration = old_run

    warnings = [e for e in launcher.events.since(0) if e.get("kind") == "warning"]
    assert len(warnings) >= 1
    assert "failed to persist to disk" in warnings[0]["message"].lower()


def test_journals_reconciles_orphaned_staging_file(tmp_path):
    """Distinguish genuine proposal hash orphans from unlinked pre-existing markdown files."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "passed").mkdir()
    (audit / "failed").mkdir()

    # 1. Genuine proposal hash artifact (64-hex chars)
    prop_hash = "a" * 64
    staged_artifact = staging / f"{prop_hash}.md"
    staged_artifact.write_text("# Proposal Note\nContent", encoding="utf-8")

    # 2. Unlinked pre-existing or manual file
    unlinked_file = staging / "manual_notes.md"
    unlinked_file.write_text("# Manual Notes\nPre-existing content", encoding="utf-8")

    launcher = _launcher(paths={
        "staging": {"root": str(staging)},
        "audit": {"root": str(audit), "passed": str(audit / "passed"), "failed": str(audit / "failed")},
    })

    j = launcher.journals()
    audits = j["audits"]
    staging_items = j["staging"]

    pending = [a for a in audits if a["outcome"] == "staged_audit_pending"]
    assert len(pending) == 1
    assert f"{prop_hash}.md" in pending[0]["title"]
    assert pending[0]["staging_available"] is True
    assert pending[0]["proposal_action"] == "CREATE"

    unlinked = [a for a in audits if a["outcome"] == "staging_unlinked"]
    assert len(unlinked) == 1
    assert "manual_notes.md" in unlinked[0]["title"]
    assert unlinked[0]["proposal_action"] == "UNKNOWN"

    staged_pending = [s for s in staging_items if s["outcome"] == "staged_audit_pending"]
    assert len(staged_pending) == 1
    assert staged_pending[0]["proposal_hash"] == prop_hash

    staged_unlinked = [s for s in staging_items if s["outcome"] == "staging_unlinked"]
    assert len(staged_unlinked) == 1
    assert staged_unlinked[0]["artifact_reference"] == "manual_notes.md"

    # Confirm neither file was deleted or altered
    assert staged_artifact.exists()
    assert unlinked_file.exists()


def test_journals_includes_emergency_audit_records_and_excludes_unrelated_logs(tmp_path):
    """Emergency audit records from errors/audit_emergency.jsonl are inspectable via journals()
    with emergency=True, while unrelated internal error logs in errors/ remain excluded."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "passed").mkdir()
    (audit / "failed").mkdir()
    errors_dir = audit / "errors"
    errors_dir.mkdir()

    # 1. Real emergency audit record in errors/audit_emergency.jsonl
    emergency_file = errors_dir / "audit_emergency.jsonl"
    emergency_entry = {
        "record_id": "rec-emergency-001",
        "run_id": "run-emergency-001",
        "proposal_hash": "e" * 64,
        "timestamp": "2026-10-24T12:00:00Z",
        "storage_category": "errors",
        "terminal_outcome": "audit_persistence_failed",
        "artifact_reference": "emergency_art.md",
        "proposal_action": "CREATE",
        "emergency": True,
        "narrative": "Emergency audit persistence fallback was activated.",
        "narrative_language": "en",
    }
    emergency_file.write_text(json.dumps(emergency_entry) + "\n", encoding="utf-8")

    # 2. Unrelated internal error file in errors/ (must NOT leak into journals)
    unrelated_log = errors_dir / "internal_traceback.log"
    unrelated_log.write_text("Traceback (most recent call last):\nException: raw secret crash details\n", encoding="utf-8")

    launcher = _launcher(paths={
        "staging": {"root": str(staging)},
        "audit": {"root": str(audit), "passed": str(audit / "passed"), "failed": str(audit / "failed")},
    })

    j = launcher.journals()
    audits = j["audits"]
    staging_items = j["staging"]

    # Emergency audit record is present and properly tagged
    emerg_audits = [a for a in audits if a.get("emergency") is True]
    assert len(emerg_audits) == 1
    ea = emerg_audits[0]
    assert ea["record_id"] == "rec-emergency-001"
    assert ea["storage_category"] == "errors"
    assert "Emergency Audit" in ea["title"]
    assert ea["emergency"] is True
    assert ea["narrative"] == "Emergency audit persistence fallback was activated."

    # Staging slot is also paired with emergency flag
    emerg_staging = [s for s in staging_items if s.get("emergency") is True]
    assert len(emerg_staging) == 1
    assert emerg_staging[0]["record_id"] == "rec-emergency-001"

    # Verify unrelated error file was NOT parsed or included
    for a in audits:
        assert "Traceback" not in a.get("title", "")
        assert "Traceback" not in a.get("summary", "")
        assert "Traceback" not in a.get("record_id", "")



def test_translate_text_honest_offline_fallback():
    """Untranslated text when offline must honestly say model is not connected and return TranslationRecord."""
    launcher = _launcher()
    result = launcher.translate_text("Unique unmapped term", target_lang="fa")
    assert isinstance(result, dict)
    translated = result.get("translated_text", "")
    assert "مدل زبانی متصل نیست" in translated
    assert "ترجمه خودکار در دسترس نیست" in translated
    assert "Unique unmapped term" in translated
    # TranslationRecord contract
    assert result.get("target_language") == "fa"
    assert result.get("source_language") == "en"
    assert result.get("source_hash") != ""
    assert result.get("status") == "unavailable"
    assert result.get("translator") == "offline-fallback"


def test_translate_endpoint_returns_translation_record(running):
    """The /api/translate HTTP endpoint returns a complete TranslationRecord payload."""
    launcher, server = running
    status, raw_data = _request(
        server,
        "POST",
        "/api/translate",
        body={"text": "Processing Result", "target_lang": "fa", "source_lang": "en"},
        token=launcher.token,
    )
    assert status == 200
    data = json.loads(raw_data)
    assert data["target_lang"] == "fa"
    assert data["target_language"] == "fa"
    assert data["source_language"] == "en"
    assert data["translated_text"] == "نتیجه پردازش"
    assert data["status"] == "current"
    assert data["translator"] == "static-dictionary-v1"
    assert data["source_hash"] != ""
    assert data["translated_hash"] != ""


def test_translate_endpoint_with_source_kind_and_id(running):
    """The /api/translate endpoint preserves source_kind and source_id provenance."""
    launcher, server = running
    status, raw_data = _request(
        server,
        "POST",
        "/api/translate",
        body={
            "text": "Audit Log",
            "target_lang": "fa",
            "source_lang": "en",
            "source_kind": "audit_narrative",
            "source_id": "rec-prov-001",
        },
        token=launcher.token,
    )
    assert status == 200
    data = json.loads(raw_data)
    assert data["source_kind"] == "audit_narrative"
    assert data["source_id"] == "rec-prov-001"
    assert data["source_text"] == "Audit Log"
    assert data["persisted"] is False


def test_translate_endpoint_same_language_identity(running):
    """Translating with same source and target language returns identity translation."""
    launcher, server = running
    sample_text = "Exact authentic observation text"
    status, raw_data = _request(
        server,
        "POST",
        "/api/translate",
        body={
            "text": sample_text,
            "target_lang": "en",
            "source_lang": "en",
            "source_kind": "proposal",
            "source_id": "prop-hash-001",
        },
        token=launcher.token,
    )
    assert status == 200
    data = json.loads(raw_data)
    assert data["translated_text"] == sample_text
    assert data["translator"] == "identity"
    assert data["status"] == "current"
    assert data["persisted"] is False
    assert data["source_kind"] == "proposal"
    assert data["source_id"] == "prop-hash-001"


def test_translate_endpoint_empty_text(running):
    """Translating empty or whitespace text returns empty string cleanly."""
    launcher, server = running
    status, raw_data = _request(
        server,
        "POST",
        "/api/translate",
        body={"text": "   ", "target_lang": "fa", "source_lang": "en"},
        token=launcher.token,
    )
    assert status == 200
    data = json.loads(raw_data)
    assert data["translated_text"] == ""
    assert data["status"] == "unavailable"


def test_translate_source_lang_detection_not_inferred_from_target(running):
    """When source_lang is omitted, source language is detected from text content,
    not inferred solely from target_lang."""
    launcher, server = running

    # 1. English text with target_lang="en" and no source_lang:
    # Must NOT infer source as "fa"; must detect "en" and return identity translation.
    status, raw_data = _request(
        server,
        "POST",
        "/api/translate",
        body={"text": "English observation without explicit source_lang", "target_lang": "en"},
        token=launcher.token,
    )
    assert status == 200
    data = json.loads(raw_data)
    assert data["source_language"] == "en"
    assert data["target_language"] == "en"
    assert data["translator"] == "identity"
    assert data["translated_text"] == "English observation without explicit source_lang"

    # 2. Persian text with target_lang="fa" and no source_lang:
    # Detects "fa" from Persian characters and returns identity translation.
    persian_text = "متن مشاهده معتبر بدون تعیین صریح زبان مبدا"
    status, raw_data = _request(
        server,
        "POST",
        "/api/translate",
        body={"text": persian_text, "target_lang": "fa"},
        token=launcher.token,
    )
    assert status == 200
    data = json.loads(raw_data)
    assert data["source_language"] == "fa"
    assert data["target_language"] == "fa"
    assert data["translator"] == "identity"
    assert data["translated_text"] == persian_text



def test_cancellation_validation_and_terminal_behavior(running):
    """Cancellation validates run_id, handles active/completed states, and protects boundaries."""
    launcher, server = running
    from src.living_authenticity.adapter.app import AdapterError

    # 1. Empty or invalid run_id raises validation_error
    with pytest.raises(AdapterError) as exc_info:
        launcher.request_cancel("")
    assert exc_info.value.kind == "validation_error"

    # 2. Unknown or inactive run_id raises validation_error
    with pytest.raises(AdapterError) as exc_info:
        launcher.request_cancel("unknown-run-999")
    assert exc_info.value.kind == "validation_error"
    assert "unknown or inactive" in exc_info.value.message

    # 3. HTTP endpoint rejects invalid/unknown run_id with 400
    status, data = _request(server, "POST", "/api/cancel", body={"run_id": "nonexistent"}, token=launcher.token)
    assert status == 400
    payload = json.loads(data)
    assert payload["error"]["kind"] == "validation_error"

    # 4. Active run cancellation sets flag
    launcher._active_run_id = "test-active-run"
    try:
        res = launcher.request_cancel("test-active-run")
        assert res["status"] == "cancel_requested"
        assert launcher._cancelled("test-active-run") is True
    finally:
        launcher._active_run_id = ""
        launcher._cancel_flags.clear()

    # 5. Already completed run reports already_completed
    launcher._reports["completed-run-1"] = {"run_id": "completed-run-1", "accepted": True}
    res_completed = launcher.request_cancel("completed-run-1")
    assert res_completed["status"] == "already_completed"
    assert res_completed["run_id"] == "completed-run-1"


def test_reveal_folder_security_and_exclusion(running, tmp_path):
    """Folder inspection rejects directory traversal and excludes hidden files."""
    launcher, server = running

    # 1. Invalid purpose or traversal is rejected by /api/folders/reveal
    status, data = _request(
        server, "POST", "/api/folders/reveal",
        body={"purpose": "../../etc"},
        token=launcher.token,
    )
    assert status == 400
    payload = json.loads(data)
    assert payload["error"]["kind"] == "validation_error"

    # 2. Configured staging folder lists files while excluding hidden files
    test_staging = tmp_path / "test_staging"
    test_staging.mkdir()
    (test_staging / "note1.md").write_text("# Note 1", encoding="utf-8")
    (test_staging / ".hidden_secret").write_text("secret", encoding="utf-8")
    (test_staging / "subfolder").mkdir()

    launcher._session_destinations["staging"] = str(test_staging)

    status, data = _request(
        server, "POST", "/api/folders/reveal",
        body={"purpose": "staging"},
        token=launcher.token,
    )
    assert status == 200
    res = json.loads(data)
    assert res["purpose"] == "staging"
    names = [item["name"] for item in res["items"]]
    assert "note1.md" in names
    assert "subfolder" in names
    assert ".hidden_secret" not in names


def test_cancel_wakes_pending_approval_waiter_promptly():
    """Cancellation must immediately unblock a thread waiting in _approval_reader."""
    import time
    import threading
    launcher = _launcher()
    run_id = "run-test-cancel-wake"
    launcher._active_run_id = run_id

    # Register a pending approval
    class MockReq:
        proposal_hash = "prop-hash-cancel-1"
        confidence_score = 0.8
    launcher.register_approval(MockReq(), None)

    reader_result = []
    start_time: float = 0.0
    end_time: float = 0.0

    def _wait_reader():
        nonlocal start_time, end_time
        start_time = time.monotonic()
        res = launcher._approval_reader()
        end_time = time.monotonic()
        reader_result.append(res)

    t = threading.Thread(target=_wait_reader)
    t.start()
    time.sleep(0.05)  # ensure thread is in wait()

    # Cancel the active run
    launcher.request_cancel(run_id)
    t.join(timeout=1.0)
    assert not t.is_alive(), "approval reader must unblock immediately on cancel"
    assert reader_result == ["cancel"], "reader must return 'cancel' signal"
    assert start_time > 0.0 and end_time >= start_time
    elapsed = end_time - start_time
    assert elapsed < 0.5, f"cancellation must wake waiter promptly, took {elapsed}s"


def test_late_approval_after_cancel_raises_conflict_409(running):
    """Calling approve after run cancellation is rejected with 409 Conflict."""
    launcher, server = running
    run_id = "run-test-late-approve"
    launcher._active_run_id = run_id

    class MockReq:
        proposal_hash = "prop-hash-late-1"
        confidence_score = 0.8
    launcher.register_approval(MockReq(), None)

    # Cancel the run
    launcher.request_cancel(run_id)

    # Late approve via HTTP POST
    status, data = _request(
        server, "POST", "/api/approve",
        body={"proposal_hash": "prop-hash-late-1", "query_unit_id": "", "decision": "approve"},
        token=launcher.token,
    )
    assert status == 409, f"expected 409 Conflict for late approval, got {status}: {data}"
    payload = json.loads(data)
    assert payload["error"]["kind"] == "conflict"


def test_journals_fails_closed_when_audit_boundary_cannot_be_created(tmp_path):
    """When audit PathBoundary cannot be constructed, journals() fails closed without reading paths."""
    from src.living_authenticity.security.path_boundary import PathBoundary

    launcher = _launcher(paths={
        "staging": {"root": str(tmp_path / "staging")},
        "audit": {"root": "\0invalid-null-byte-path"},
    })
    j = launcher.journals()
    assert j["audits"] == []
    assert j["staging"] == []


def test_journals_staging_path_boundary_enforcement(tmp_path):
    """Staging artifacts outside staging_root boundary are rejected and not read into excerpts."""
    staging = tmp_path / "staging"
    staging.mkdir()
    audit = tmp_path / "audit"
    audit.mkdir()
    (audit / "passed").mkdir()

    outside_file = tmp_path / "outside_secret.md"
    outside_file.write_text("SUPER SECRET DATA OUTSIDE STAGING", encoding="utf-8")

    # Write audit log containing artifact_path pointing outside staging root
    passed_log = audit / "passed" / "audit.jsonl"
    entry = {
        "record_id": "rec-boundary-001",
        "run_id": "run-b-001",
        "proposal_hash": "a" * 64,
        "timestamp": "2026-10-24T12:00:00Z",
        "storage_category": "passed",
        "artifact_reference": "outside_secret.md",
        "artifact_path": str(outside_file),
        "proposal_action": "CREATE",
    }
    passed_log.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    launcher = _launcher(paths={
        "staging": {"root": str(staging)},
        "audit": {"root": str(audit), "passed": str(audit / "passed")},
    })
    j = launcher.journals()
    assert len(j["audits"]) == 1
    staging_slot = j["staging"][0]
    # Outside artifact is rejected by PathBoundary: not staged, no secret excerpt leaked
    assert staging_slot["staging_available"] is False
    assert "SUPER SECRET DATA" not in staging_slot["excerpt"]
    assert staging_slot["artifact_path"] == ""

