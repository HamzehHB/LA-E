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


def test_non_create_proposal_web_governance_path(tmp_path):
    """End-to-end integration test for non-CREATE proposals in a background/web context.

    Verifies:
    1. A non-CREATE proposal running in the background web server does not hang on terminal input.
    2. Emits human_review_required event with canonical proposal_hash and action NEEDS_REVIEW.
    3. Registers pending_approval on Launcher.
    4. Approving via /api/approve is blocked (non-executable action cannot be approved).
    5. Acknowledging via POST /api/run/accept emits approval_decided event and resolves pending review.
    6. Run completes cleanly (run_completed emitted) with zero staging writes.
    7. Held audit record is durably persisted to audit/failed/ with matching proposal_hash and
       human_review_state='acknowledged_non_executable'.
    """
    import time
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _make_needs_review_unit():
        query = KnowledgeUnit(
            id="ku-transport-needs-review", source="chat_input:inp-001",
            original_text="Ambiguous input text needing human review before staging.",
            meaning="Ambiguous input text needing human review before staging.",
            cleaned_text="Ambiguous input text needing human review before staging.",
            normalized_text="Ambiguous input text needing human review before staging.",
            position=0,
        )
        proposal = Proposal(
            query_unit_id="ku-transport-needs-review", query_source="chat_input:inp-001",
            query_position=0, action="NEEDS_REVIEW",
            title="Needs Review Concept",
            destination="Notes/NeedsReview.md",
            reason="Ambiguity detected during analysis",
            classification_type="Observation",
        )
        classification = ClassificationResult(
            unit_id="ku-transport-needs-review", source="chat_input:inp-001", position=0,
            proposed_type="Observation",
            rationale="Ambiguous text categorized as observation",
        )
        candidate = _Obj(unit_id="vault-u1", source="vault/note.md",
                         position=0, overlap_score=2, text="existing note excerpt")
        retrieval = _Obj(candidates=(candidate,), note="lexical retrieval note")
        comparison = _Obj(candidate_unit_id="vault-u1", category="related",
                          shared_terms=("ambiguous", "review"))
        relation = _Obj(proposals=())
        core = _Obj(relevance="unresolved", basis="unresolved core relevance")
        confidence = ConfidenceAssessment(
            query_unit_id="ku-transport-needs-review", query_source="chat_input:inp-001",
            query_position=0, proposal_action="NEEDS_REVIEW",
            level="weak", basis="synthetic weak confidence basis",
        )
        filter_outcome = _Obj(verdict="needs_review", basis="synthetic filter review basis")

        class _StubUnitResult:
            def __init__(self):
                self.query_unit = query
                self.proposal = proposal
                self.classification = classification
                self.retrieval_result = retrieval
                self.comparisons = (comparison,)
                self.relation_result = relation
                self.core_analysis = core
                self.confidence = confidence
                self.filter_outcome = filter_outcome

        return _StubUnitResult()

    class _MockPipeline:
        def __init__(self, stage_observer=None):
            self._stage_observer = stage_observer
            self.ingestion = EvidenceFirstPipeline(stage_observer=stage_observer).ingestion

        def run_ingestion(self, ingestion, corpus=(), core_units=()):
            class _Result:
                units = [_make_needs_review_unit()]
            return _Result()

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    launcher = _launcher(
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {"root": str(audit_dir)},
        },
        pipeline_factory=lambda sink: _MockPipeline(stage_observer=sink),
        synthesis_factory=lambda confirm=None, config=None: SyntheticSynthesis(
            title="Needs Review Concept",
            body="Synthesized candidate body requiring human review.",
        ),
    )
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        # Start a real run through HTTP POST /api/run
        valid_input = "This is a sufficiently long input that satisfies all length constraints and establishes provenance cleanly."
        status, data = _request(
            server, "POST", "/api/run",
            body={"input_text": valid_input, "source_mode": "examples"},
            token=launcher.token,
        )
        assert status == 202, f"expected 202 from /api/run, got {status}: {data}"
        run_id = json.loads(data)["run_id"]

        # Wait for the worker thread to reach human_review and register pending approval
        pending = None
        for _ in range(50):
            with launcher._lock:
                pending = launcher._pending_approval
                if pending is not None:
                    break
            time.sleep(0.1)
        assert pending is not None, "worker thread must register pending approval without hanging on terminal input"

        # Verify the emitted human_review_required event contract
        review_events = [e for e in launcher.events.since(0) if e.get("kind") == "human_review_required"]
        assert len(review_events) == 1, f"expected 1 human_review_required event, got {review_events}"
        rev = review_events[0]
        assert rev["proposal_action"] == "NEEDS_REVIEW"
        assert rev["proposal_hash"] != "", "proposal_hash must be a canonical hash, not empty string"
        assert rev["query_unit_id"] == "ku-transport-needs-review"
        prop_hash = rev["proposal_hash"]

        # Invariant: Attempting to approve a non-executable proposal via /api/approve MUST fail closed
        status, data = _request(
            server, "POST", "/api/approve",
            body={"proposal_hash": prop_hash, "query_unit_id": "ku-transport-needs-review", "decision": "approve"},
            token=launcher.token,
        )
        assert status == 400, f"approving non-CREATE proposal must return 400, got {status}: {data}"
        payload = json.loads(data)
        assert "cannot be approved" in payload["error"]["message"]

        # Acknowledge the non-executable review findings via POST /api/run/accept
        status, data = _request(
            server, "POST", "/api/run/accept",
            body={"run_id": run_id, "proposal_hash": prop_hash},
            token=launcher.token,
        )
        assert status == 200, f"accept_proposal must return 200, got {status}: {data}"
        resp = json.loads(data)
        assert resp["accepted"] is True
        assert resp["action"] == "NEEDS_REVIEW"
        assert resp["proposal_hash"] == prop_hash

        # Wait for run to finish cleanly
        summary = None
        for _ in range(50):
            with launcher._lock:
                if run_id in launcher._reports:
                    summary = launcher._reports[run_id]
                    break
            time.sleep(0.1)
        assert summary is not None, "run must complete cleanly after acknowledgment"
        assert summary["executed"] == 0, "non-CREATE proposal must not execute"
        assert summary["held"] == 1, "non-CREATE proposal must be counted as held"
        assert summary["audit_persisted"] is True, "held audit record must be persisted"

        # Verify approval_decided and run_completed events were emitted
        decided_events = [e for e in launcher.events.since(0) if e.get("kind") == "approval_decided"]
        assert len(decided_events) == 1
        assert decided_events[0]["message"] == "acknowledged_needs_review"
        assert decided_events[0]["proposal_hash"] == prop_hash

        completed_events = [e for e in launcher.events.since(0) if e.get("kind") == "run_completed"]
        assert len(completed_events) == 1

        # Verify zero staging writes
        assert list(staging_dir.iterdir()) == [], "no files should be written to staging for non-CREATE proposals"

        # Verify durable audit record persistence in audit/failed/
        failed_log = audit_dir / "failed" / "audit.jsonl"
        assert failed_log.exists(), "held non-executable audit record must be written to audit/failed/audit.jsonl"
        records = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").strip().splitlines()]
        held_records = [r for r in records if r.get("proposal_hash") == prop_hash]
        assert len(held_records) == 1, f"expected held record with proposal_hash {prop_hash}, found: {records}"
        rec = held_records[0]
        assert rec["proposal_action"] == "NEEDS_REVIEW"
        assert rec["terminal_outcome"] == "held_non_executable"
        assert rec["human_review_state"] == "acknowledged_non_executable"
        assert rec["approval_approved"] is False
        assert rec["execution_executed"] is False
    finally:
        server.shutdown()


def test_non_create_proposal_rejection_and_cancellation(tmp_path):
    """Verifies rejection and cancellation pathways for non-CREATE proposals."""
    import time
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _make_unit():
        query = KnowledgeUnit(
            id="ku-transport-rej", source="chat_input:inp-002",
            original_text="Ambiguous input text needing rejection test.",
            meaning="Ambiguous input text needing rejection test.",
            cleaned_text="Ambiguous input text needing rejection test.",
            normalized_text="Ambiguous input text needing rejection test.",
            position=0,
        )
        proposal = Proposal(
            query_unit_id="ku-transport-rej", query_source="chat_input:inp-002",
            query_position=0, action="DO_NOT_IMPORT",
            title="Do Not Import Note",
            destination="Notes/DoNotImport.md",
            reason="Explicitly marked do not import",
            classification_type="Observation",
        )
        classification = ClassificationResult(
            unit_id="ku-transport-rej", source="chat_input:inp-002", position=0,
            proposed_type="Observation", rationale="Categorized as observation",
        )
        candidate = _Obj(unit_id="v1", source="vault/v1.md", position=0, overlap_score=1, text="text")
        retrieval = _Obj(candidates=(candidate,), note="note")
        comparison = _Obj(candidate_unit_id="v1", category="related", shared_terms=())
        confidence = ConfidenceAssessment(
            query_unit_id="ku-transport-rej", query_source="chat_input:inp-002",
            query_position=0, proposal_action="DO_NOT_IMPORT",
            level="low", basis="low confidence",
        )
        filter_outcome = _Obj(verdict="needs_review", basis="filter basis")

        class _StubUnitResult:
            def __init__(self):
                self.query_unit = query
                self.proposal = proposal
                self.classification = classification
                self.retrieval_result = retrieval
                self.comparisons = (comparison,)
                self.relation_result = _Obj(proposals=())
                self.core_analysis = _Obj(relevance="unresolved", basis="basis")
                self.confidence = confidence
                self.filter_outcome = filter_outcome

        return _StubUnitResult()

    class _MockPipeline:
        def __init__(self, stage_observer=None):
            self._stage_observer = stage_observer
            self.ingestion = EvidenceFirstPipeline(stage_observer=stage_observer).ingestion

        def run_ingestion(self, ingestion, corpus=(), core_units=()):
            class _Result:
                units = [_make_unit()]
            return _Result()

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    launcher = _launcher(
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {"root": str(audit_dir)},
        },
        pipeline_factory=lambda sink: _MockPipeline(stage_observer=sink),
        synthesis_factory=lambda confirm=None, config=None: SyntheticSynthesis(
            title="Do Not Import Note",
            body="Synthesized body.",
        ),
    )
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        # Test 1: Rejection via /api/approve with decision="reject"
        valid_input = "This is a sufficiently long input that satisfies all length constraints and establishes provenance cleanly."
        status, data = _request(
            server, "POST", "/api/run",
            body={"input_text": valid_input, "source_mode": "examples"},
            token=launcher.token,
        )
        assert status == 202
        run_id = json.loads(data)["run_id"]

        for _ in range(50):
            with launcher._lock:
                if launcher._pending_approval is not None:
                    break
            time.sleep(0.1)
        assert launcher._pending_approval is not None
        prop_hash = launcher._pending_approval.key

        status, data = _request(
            server, "POST", "/api/approve",
            body={"proposal_hash": prop_hash, "query_unit_id": "ku-transport-rej", "decision": "reject"},
            token=launcher.token,
        )
        assert status == 200

        for _ in range(50):
            with launcher._lock:
                if run_id in launcher._reports:
                    break
            time.sleep(0.1)
        assert run_id in launcher._reports

        failed_log = audit_dir / "failed" / "audit.jsonl"
        assert failed_log.exists()
        records = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").strip().splitlines()]
        rej_records = [r for r in records if r.get("proposal_hash") == prop_hash]
        assert len(rej_records) == 1
        assert rej_records[0]["terminal_outcome"] == "rejected"
        assert rej_records[0]["human_review_state"] == "rejected"
        assert rej_records[0]["approval_approved"] is False

        # Test 2: Cancellation via /api/run/stop
        status, data = _request(
            server, "POST", "/api/run",
            body={"input_text": valid_input, "source_mode": "examples"},
            token=launcher.token,
        )
        assert status == 202
        run_id_2 = json.loads(data)["run_id"]

        for _ in range(50):
            with launcher._lock:
                if launcher._pending_approval is not None:
                    break
            time.sleep(0.1)
        assert launcher._pending_approval is not None
        prop_hash_2 = launcher._pending_approval.key

        # Cancel run via /api/cancel
        status, data = _request(
            server, "POST", "/api/cancel",
            body={"run_id": run_id_2},
            token=launcher.token,
        )
        assert status == 200

        for _ in range(50):
            with launcher._lock:
                if run_id_2 in launcher._reports:
                    break
            time.sleep(0.1)
        assert run_id_2 in launcher._reports

        # Verify operational error routing for cancellation (errors/)
        errors_log = audit_dir / "errors" / "audit.jsonl"
        assert errors_log.exists()
        records_err = [json.loads(line) for line in errors_log.read_text(encoding="utf-8").strip().splitlines()]
        cancel_records = [r for r in records_err if r.get("proposal_hash") == prop_hash_2]
        assert len(cancel_records) == 1
        assert cancel_records[0]["terminal_outcome"] == "cancelled"
        assert cancel_records[0]["human_review_state"] == "cancelled"
    finally:
        server.shutdown()


def test_non_interactive_web_execution_never_calls_terminal_input(tmp_path, monkeypatch):
    """Verify web-server and non-interactive paths never invoke builtins.input under any circumstance."""
    import time

    def _fail_on_input(*args, **kwargs):
        pytest.fail("builtins.input() was called in web/non-interactive execution")

    monkeypatch.setattr("builtins.input", _fail_on_input)

    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _make_unit():
        query = KnowledgeUnit(
            id="ku-transport-no-input", source="source.md",
            original_text="text for no input test",
            meaning="text for no input test",
            cleaned_text="text for no input test",
            normalized_text="text for no input test",
            position=0,
        )
        proposal = Proposal(
            query_unit_id="ku-transport-no-input",
            query_source="source.md",
            query_position=0,
            action="NEEDS_REVIEW",
            title="Needs Review Note",
            destination="destination.md",
            reason="Ambiguous input text needing human review",
            classification_type="Observation",
        )
        classification = ClassificationResult(
            unit_id="ku-transport-no-input", source="source.md", position=0,
            proposed_type="Observation", rationale="basis",
        )
        candidate = _Obj(unit_id="vault-u1", source="vault/note.md",
                         position=0, overlap_score=2, text="existing note excerpt")
        retrieval = _Obj(candidates=(candidate,), note="retrieval note")
        comparison = _Obj(candidate_unit_id="vault-u1", category="related", shared_terms=["term"])
        confidence = ConfidenceAssessment(
            query_unit_id="ku-transport-no-input", query_source="source.md",
            query_position=0, proposal_action="NEEDS_REVIEW", level="weak", basis="basis",
        )
        filter_outcome = _Obj(verdict="needs_review", basis="filter basis")

        class _StubUnitResult:
            def __init__(self):
                self.query_unit = query
                self.proposal = proposal
                self.classification = classification
                self.retrieval_result = retrieval
                self.comparisons = (comparison,)
                self.relation_result = _Obj(proposals=())
                self.core_analysis = _Obj(relevance="unresolved", basis="basis")
                self.confidence = confidence
                self.filter_outcome = filter_outcome

        return _StubUnitResult()

    class _MockPipeline:
        def __init__(self, stage_observer=None):
            self._stage_observer = stage_observer
            self.ingestion = EvidenceFirstPipeline(stage_observer=stage_observer).ingestion

        def run_ingestion(self, ingestion, corpus=(), core_units=()):
            class _Result:
                units = [_make_unit()]
            return _Result()

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    launcher = _launcher(
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {"root": str(audit_dir)},
        },
        pipeline_factory=lambda sink: _MockPipeline(stage_observer=sink),
        synthesis_factory=lambda confirm=None, config=None: SyntheticSynthesis(
            title="Needs Review Note",
            body="Synthesized body text.",
        ),
    )
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        valid_input = "Sufficiently long input that satisfies all length constraints and establishes provenance cleanly."
        status, data = _request(
            server, "POST", "/api/run",
            body={"input_text": valid_input, "source_mode": "examples"},
            token=launcher.token,
        )
        assert status == 202
        run_id = json.loads(data)["run_id"]

        for _ in range(50):
            with launcher._lock:
                if launcher._pending_approval is not None:
                    break
            time.sleep(0.1)
        assert launcher._pending_approval is not None
        prop_hash = launcher._pending_approval.key

        # Acknowledge review via /api/run/accept
        status, data = _request(
            server, "POST", "/api/run/accept",
            body={"run_id": run_id, "proposal_hash": prop_hash},
            token=launcher.token,
        )
        assert status == 200
        assert json.loads(data)["accepted"] is True

        for _ in range(50):
            with launcher._lock:
                if run_id in launcher._reports:
                    break
            time.sleep(0.1)
        assert run_id in launcher._reports

        failed_log = audit_dir / "failed" / "audit.jsonl"
        assert failed_log.exists()
        records = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").strip().splitlines()]
        held_records = [r for r in records if r.get("proposal_hash") == prop_hash]
        assert len(held_records) == 1
        assert held_records[0]["terminal_outcome"] == "held_non_executable"
        assert held_records[0]["human_review_state"] == "acknowledged_non_executable"
        assert held_records[0]["approval_approved"] is False
    finally:
        server.shutdown()


def test_review_channel_unavailable_fails_closed_safely(tmp_path):
    """When review reader is missing in a web/background run, fail closed without hanging."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    query = KnowledgeUnit(
        id="ku-unavailable", source="source.md",
        original_text="text for unavailable test",
        meaning="text for unavailable test",
        cleaned_text="text for unavailable test",
        normalized_text="text for unavailable test",
        position=0,
    )
    proposal = Proposal(
        query_unit_id="ku-unavailable",
        query_source="source.md",
        query_position=0,
        action="NEEDS_REVIEW",
        title="Unavailable Review Note",
        destination="dest.md",
        reason="Ambiguous input",
        classification_type="Observation",
    )
    classification = ClassificationResult(
        unit_id="ku-unavailable", source="source.md", position=0,
        proposed_type="Observation", rationale="b",
    )
    unit_result = _Obj(
        query_unit=query,
        proposal=proposal,
        classification=classification,
        retrieval_result=_Obj(candidates=(), note="n"),
        comparisons=(),
        relation_result=_Obj(proposals=()),
        core_analysis=_Obj(relevance="unresolved", basis="b"),
        confidence=ConfidenceAssessment(
            query_unit_id="ku-unavailable", query_source="source.md",
            query_position=0, proposal_action="NEEDS_REVIEW", level="weak", basis="b",
        ),
        filter_outcome=_Obj(verdict="needs_review", basis="b"),
    )

    emitted_events = []
    entries = []
    audits = []

    _handle_unit(
        "child.md", unit_result,
        gate=None,
        validator=None,
        runner=None,
        pinned_staging="",
        guarded=(),
        schema_version="",
        approval_reader=None,  # Intentionally missing review reader
        entries=entries,
        audits=audits,
        authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        run_id="run-web-unavailable",  # Declares web/background context
        event_sink=lambda ev: emitted_events.append(ev),
        interactive=False,
    )

    assert len(entries) == 1
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].reason == "review channel unavailable for proposal action NEEDS_REVIEW"

    # Verify observable safe terminal stop was emitted
    stopped_events = [e for e in emitted_events if e.get("kind") == "stopped" and e.get("stage") == "human_review"]
    assert len(stopped_events) == 1
    assert "review channel unavailable" in stopped_events[0].get("message", "")

    # Verify audit record reflects truthful terminal state
    assert len(audits) == 1
    assert audits[0].terminal_outcome == "held_non_executable"
    assert audits[0].human_review_state == "review_channel_unavailable"


def test_audit_persistence_failure_is_observable_and_safe(tmp_path):
    """When audit persistence fails, surface an operational failure without claiming durability."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    query = KnowledgeUnit(
        id="ku-audit-fail", source="source.md",
        original_text="text for audit fail test",
        meaning="text for audit fail test",
        cleaned_text="text for audit fail test",
        normalized_text="text for audit fail test",
        position=0,
    )
    proposal = Proposal(
        query_unit_id="ku-audit-fail",
        query_source="source.md",
        query_position=0,
        action="NEEDS_REVIEW",
        title="Audit Fail Review Note",
        destination="dest.md",
        reason="Ambiguous input",
        classification_type="Observation",
    )
    classification = ClassificationResult(
        unit_id="ku-audit-fail", source="source.md", position=0,
        proposed_type="Observation", rationale="b",
    )
    unit_result = _Obj(
        query_unit=query,
        proposal=proposal,
        classification=classification,
        retrieval_result=_Obj(candidates=(), note="n"),
        comparisons=(),
        relation_result=_Obj(proposals=()),
        core_analysis=_Obj(relevance="unresolved", basis="b"),
        confidence=ConfidenceAssessment(
            query_unit_id="ku-audit-fail", query_source="source.md",
            query_position=0, proposal_action="NEEDS_REVIEW", level="weak", basis="b",
        ),
        filter_outcome=_Obj(verdict="needs_review", basis="b"),
    )

    class _FailingWriter:
        def append(self, record):
            raise OSError("Disk full: write failed")
        def append_emergency(self, record, failure_reason=""):
            raise OSError("Emergency partition also unreachable")

    emitted_warnings = []
    receipts = []
    audits = []

    _handle_unit(
        "child.md", unit_result,
        gate=None,
        validator=None,
        runner=None,
        pinned_staging="",
        guarded=(),
        schema_version="",
        approval_reader=lambda: "acknowledge",
        entries=[],
        audits=audits,
        authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=_FailingWriter(),
        audit_receipts=receipts,
        event_sink=lambda ev: emitted_warnings.append(ev) if ev.get("kind") == "warning" else None,
        interactive=False,
    )

    # Observable warning was emitted
    assert len(emitted_warnings) >= 1
    assert any("audit_persistence" in w.get("stage", "") for w in emitted_warnings)

    # Failed receipt was recorded with durability="failed"
    assert len(receipts) >= 1
    failed_receipt = receipts[0]
    assert failed_receipt.get("persisted") is False
    assert failed_receipt.get("durability") == "failed"


def test_timeout_review_lifecycle_releases_pending_state_and_emits_event(tmp_path):
    """When a review times out, release pending state, emit timeout event, and record outcome."""
    import time
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.orchestration import EvidenceFirstPipeline
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    def _make_unit():
        query = KnowledgeUnit(
            id="ku-timeout-test", source="source.md",
            original_text="text for timeout test",
            meaning="text for timeout test",
            cleaned_text="text for timeout test",
            normalized_text="text for timeout test",
            position=0,
        )
        proposal = Proposal(
            query_unit_id="ku-timeout-test",
            query_source="source.md",
            query_position=0,
            action="NEEDS_REVIEW",
            title="Timeout Review Note",
            destination="destination.md",
            reason="Ambiguous input",
            classification_type="Observation",
        )
        classification = ClassificationResult(
            unit_id="ku-timeout-test", source="source.md", position=0,
            proposed_type="Observation", rationale="basis",
        )
        class _StubUnitResult:
            def __init__(self):
                self.query_unit = query
                self.proposal = proposal
                self.classification = classification
                self.retrieval_result = _Obj(candidates=(), note="retrieval note")
                self.comparisons = ()
                self.relation_result = _Obj(proposals=())
                self.core_analysis = _Obj(relevance="unresolved", basis="basis")
                self.confidence = ConfidenceAssessment(
                    query_unit_id="ku-timeout-test", query_source="source.md",
                    query_position=0, proposal_action="NEEDS_REVIEW", level="weak", basis="basis",
                )
                self.filter_outcome = _Obj(verdict="needs_review", basis="filter basis")

        return _StubUnitResult()

    class _MockPipeline:
        def __init__(self, stage_observer=None):
            self._stage_observer = stage_observer
            self.ingestion = EvidenceFirstPipeline(stage_observer=stage_observer).ingestion

        def run_ingestion(self, ingestion, corpus=(), core_units=()):
            class _Result:
                units = [_make_unit()]
            return _Result()

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    launcher = _launcher(
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {"root": str(audit_dir)},
        },
        pipeline_factory=lambda sink: _MockPipeline(stage_observer=sink),
        synthesis_factory=lambda confirm=None, config=None: SyntheticSynthesis(
            title="Needs Review Note",
            body="Synthesized body text.",
        ),
    )
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        valid_input = "Sufficiently long input that satisfies all length constraints and establishes provenance cleanly."
        status, data = _request(
            server, "POST", "/api/run",
            body={"input_text": valid_input, "source_mode": "examples"},
            token=launcher.token,
        )
        assert status == 202
        run_id = json.loads(data)["run_id"]

        for _ in range(50):
            with launcher._lock:
                if launcher._pending_approval is not None:
                    break
            time.sleep(0.1)
        assert launcher._pending_approval is not None
        prop_hash = launcher._pending_approval.key

        # Simulate timeout on the pending approval
        with launcher._lock:
            pending = launcher._pending_approval
            if pending is not None:
                pending.resolve("timeout")

        for _ in range(50):
            with launcher._lock:
                if run_id in launcher._reports:
                    break
            time.sleep(0.1)
        assert run_id in launcher._reports

        failed_log = audit_dir / "failed" / "audit.jsonl"
        assert failed_log.exists()
        records = [json.loads(line) for line in failed_log.read_text(encoding="utf-8").strip().splitlines()]
        timeout_records = [r for r in records if r.get("proposal_hash") == prop_hash]
        assert len(timeout_records) == 1
        assert timeout_records[0]["terminal_outcome"] == "timed_out"
        assert timeout_records[0]["human_review_state"] == "timed_out"
        assert timeout_records[0]["approval_approved"] is False
    finally:
        server.shutdown()


def _make_non_create_unit_result():
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
    from src.living_authenticity.knowledge.analysis.classification.result import ClassificationResult
    from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
    from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment

    class _Obj:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    query = KnowledgeUnit(
        id="ku-integrity-test", source="source.md",
        original_text="text for decision integrity test",
        meaning="text for decision integrity test",
        cleaned_text="text for decision integrity test",
        normalized_text="text for decision integrity test",
        position=0,
    )
    proposal = Proposal(
        query_unit_id="ku-integrity-test",
        query_source="source.md",
        query_position=0,
        action="NEEDS_REVIEW",
        title="Review Decision Integrity Note",
        destination="destination.md",
        reason="Ambiguous categorization",
        classification_type="Observation",
    )
    classification = ClassificationResult(
        unit_id="ku-integrity-test", source="source.md", position=0,
        proposed_type="Observation", rationale="basis",
    )
    return _Obj(
        query_unit=query,
        proposal=proposal,
        classification=classification,
        retrieval_result=_Obj(candidates=(), note="retrieval note"),
        comparisons=(),
        relation_result=_Obj(proposals=()),
        core_analysis=_Obj(relevance="unresolved", basis="basis"),
        confidence=ConfidenceAssessment(
            query_unit_id="ku-integrity-test", query_source="source.md",
            query_position=0, proposal_action="NEEDS_REVIEW", level="weak", basis="basis",
        ),
        filter_outcome=_Obj(verdict="needs_review", basis="filter basis"),
    )


def test_non_create_empty_reader_records_empty_input_held(tmp_path):
    """Empty reader input is recorded as empty_input, never false acknowledgment."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    receipts = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=receipts,
        interactive=True,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "empty_input"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False
    assert entries[0].execution_executed is False
    assert entries[0].artifact_reference == ""

    assert len(audits) == 1
    assert audits[0].human_review_state == "empty_input"
    assert audits[0].validation_result == "held_non_executable"
    assert audits[0].approval_approved is False


def test_non_create_reader_exception_records_reader_exception(tmp_path):
    """Exceptions from reader are recorded as reader_exception, never false acknowledgment."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    def _broken_reader():
        raise RuntimeError("simulated I/O disconnect")

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=_broken_reader,
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=[],
        interactive=True,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "reader_exception"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False
    assert entries[0].execution_executed is False

    assert len(audits) == 1
    assert audits[0].human_review_state == "reader_exception"


def test_non_create_eof_records_eof_held(tmp_path):
    """EOF from reader is recorded as eof, never false acknowledgment."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    def _eof_reader():
        raise EOFError("input stream closed")

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=_eof_reader,
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=[],
        interactive=True,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "eof"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False

    assert len(audits) == 1
    assert audits[0].human_review_state == "eof"


def test_non_create_invalid_decision_records_invalid_decision(tmp_path):
    """Unrecognized human input is recorded as invalid_decision and held non-executable."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "approve",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=[],
        interactive=True,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "invalid_decision"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False
    assert entries[0].execution_executed is False

    assert len(audits) == 1
    assert audits[0].human_review_state == "invalid_decision"


def test_non_create_explicit_ack_records_acknowledged(tmp_path):
    """Explicit human acknowledgment produces acknowledged_non_executable."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "acknowledge",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=[],
        interactive=True,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "acknowledged_non_executable"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False
    assert entries[0].execution_executed is False

    assert len(audits) == 1
    assert audits[0].human_review_state == "acknowledged_non_executable"


def test_non_create_headless_records_not_requested(tmp_path):
    """Headless batch run without reader records human_review_state as not_requested."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=None,
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=[],
        interactive=False,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "not_requested"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False
    assert entries[0].execution_executed is False

    assert len(audits) == 1
    assert audits[0].human_review_state == "not_requested"


def test_non_create_channel_unavailable_records_review_channel_unavailable(tmp_path):
    """Web run without reader records review_channel_unavailable."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=None,
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=AuditWriter(str(audit_root)),
        audit_receipts=[],
        run_id="run-web-channel-test",
        interactive=False,
    )

    assert len(entries) == 1
    assert entries[0].human_review_state == "review_channel_unavailable"
    assert entries[0].outcome == "held_non_executable"
    assert entries[0].approval_approved is False

    assert len(audits) == 1
    assert audits[0].human_review_state == "review_channel_unavailable"


def test_held_record_persistence_success(tmp_path):
    """When held record persistence succeeds, durability is written and audit_persisted is True."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter
    from src.living_authenticity.knowledge.integration.outcome import IntegrationRunReport

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    receipts = []
    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    writer = AuditWriter(str(audit_root))

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "acknowledge",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=writer,
        audit_receipts=receipts,
        interactive=False,
    )

    assert len(receipts) == 1
    assert receipts[0]["persisted"] is True
    assert receipts[0]["durability"] == "written"

    failed_log = audit_root / "failed" / "audit.jsonl"
    assert failed_log.exists()
    records = [json.loads(l) for l in failed_log.read_text(encoding="utf-8").strip().splitlines()]
    assert len(records) == 1
    assert records[0]["validation_result"] == "held_non_executable"

    launcher = _launcher()
    rep = IntegrationRunReport(accepted=True, units=tuple(entries), audits=tuple(audits), audit_receipts=tuple(receipts))
    summary = launcher._report_summary(rep)
    assert summary["audit_persisted"] is True
    assert summary["audit_warning"] == ""


def test_held_record_persistence_emergency_fallback(tmp_path):
    """When primary append fails but emergency succeeds, durability is confirmed and audit_persisted is True."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.governance.audit import AuditWriter
    from src.living_authenticity.knowledge.integration.outcome import IntegrationRunReport

    audit_root = tmp_path / "audit"
    audit_root.mkdir()
    real_writer = AuditWriter(str(audit_root))

    class _EmergencyFallbackWriter:
        def append(self, record):
            raise OSError("Primary write disk error")
        def append_emergency(self, record, failure_reason=""):
            return real_writer.append_emergency(record, failure_reason=failure_reason)

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    receipts = []

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "acknowledge",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=_EmergencyFallbackWriter(),
        audit_receipts=receipts,
        interactive=False,
    )

    assert len(receipts) == 1
    assert receipts[0]["persisted"] is True
    assert receipts[0]["durability"] == "confirmed"
    assert receipts[0]["emergency"] is True

    emergency_log = audit_root / "errors" / "audit_emergency.jsonl"
    assert emergency_log.exists()

    launcher = _launcher()
    rep = IntegrationRunReport(accepted=True, units=tuple(entries), audits=tuple(audits), audit_receipts=tuple(receipts))
    summary = launcher._report_summary(rep)
    assert summary["audit_persisted"] is True


def test_held_record_persistence_complete_failure(tmp_path):
    """When primary and emergency persistence both fail, audit_persisted is False and warning is emitted."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.integration.outcome import IntegrationRunReport

    class _TotallyFailingWriter:
        def append(self, record):
            raise OSError("Primary disk full")
        def append_emergency(self, record, failure_reason=""):
            raise OSError("Emergency partition offline")

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    receipts = []
    emitted = []

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "acknowledge",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=_TotallyFailingWriter(),
        audit_receipts=receipts,
        event_sink=lambda ev: emitted.append(ev) if ev.get("kind") == "warning" else None,
        interactive=False,
    )

    assert len(receipts) == 1
    assert receipts[0]["persisted"] is False
    assert receipts[0]["durability"] == "failed"
    assert any("audit_persistence" in e.get("stage", "") for e in emitted)

    launcher = _launcher()
    rep = IntegrationRunReport(accepted=True, units=tuple(entries), audits=tuple(audits), audit_receipts=tuple(receipts))
    summary = launcher._report_summary(rep)
    assert summary["audit_persisted"] is False
    assert "Proposal held, but 1 audit record(s) failed to persist to disk" in summary["audit_warning"]


def test_held_record_persistence_unconfigured(tmp_path):
    """When audit infrastructure is unconfigured, receipts record unconfigured and audit_persisted is False."""
    from tests._synthetic_synthesis import SyntheticSynthesis
    from src.living_authenticity.knowledge.integration.unit_loop import _handle_unit
    from src.living_authenticity.knowledge.integration.outcome import IntegrationRunReport

    unit_result = _make_non_create_unit_result()
    entries = []
    audits = []
    receipts = []
    emitted = []

    _handle_unit(
        "note.md", unit_result,
        gate=None, validator=None, runner=None,
        pinned_staging=str(tmp_path / "staging"), guarded=(), schema_version="",
        approval_reader=lambda: "acknowledge",
        entries=entries, audits=audits, authorized_staging="",
        synthesis=SyntheticSynthesis(title="T", body="B"),
        audit_writer=None,
        audit_receipts=receipts,
        event_sink=lambda ev: emitted.append(ev) if ev.get("kind") == "warning" else None,
        interactive=False,
    )

    assert len(receipts) == 1
    assert receipts[0]["persisted"] is False
    assert receipts[0]["durability"] == "unconfigured"
    assert receipts[0]["mode"] == "unconfigured"

    launcher = _launcher()
    rep = IntegrationRunReport(accepted=True, units=tuple(entries), audits=tuple(audits), audit_receipts=tuple(receipts))
    summary = launcher._report_summary(rep)
    assert summary["audit_persisted"] is False
    assert "Audit infrastructure is unconfigured" in summary["audit_warning"]


def test_real_pipeline_runtime_handoff_and_stale_decision_rejection(tmp_path):
    """Execute end-to-end run with genuine EvidenceFirstPipeline, tracing review handoff,
    proposal-hash continuity, durable audit record, and rejection of stale/duplicate decisions."""
    import time
    from tests._synthetic_synthesis import SyntheticSynthesis

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()

    # Use genuine EvidenceFirstPipeline (pipeline_factory=None)
    launcher = _launcher(
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {"root": str(audit_dir)},
        },
        pipeline_factory=None,
        synthesis_factory=lambda confirm=None, config=None: SyntheticSynthesis(
            title="Genuine Qualitative Research Note",
            body="Synthesized phenomenological observation text.",
        ),
    )
    server = LauncherServer(launcher)
    server.serve_in_thread()

    try:
        # Authentic qualitative research text satisfying length constraints
        input_text = (
            "Authentic qualitative research inquiry into experiential self-awareness and "
            "ontological congruence across empirical modalities, providing sufficient textual depth "
            "to comfortably exercise extraction, lexical indexing, proposal generation, and governance."
        )

        status, data = _request(
            server, "POST", "/api/run",
            body={"input_text": input_text, "source_mode": "examples"},
            token=launcher.token,
        )
        assert status == 202
        run_id = json.loads(data)["run_id"]

        # Wait for human_review_required event and pending approval registration
        for _ in range(60):
            with launcher._lock:
                if launcher._pending_approval is not None:
                    break
            time.sleep(0.1)

        assert launcher._pending_approval is not None, "Pending approval was not registered by real pipeline"
        proposal_hash = launcher._pending_approval.key
        query_unit_id = launcher._pending_approval.payload.get("query_unit_id", "")
        action = launcher._pending_approval.payload.get("proposal_action", "")
        assert proposal_hash, "Pending approval key must be a valid canonical proposal hash"

        # Verify event stream received human_review_required with exact proposal_hash
        events = launcher.events.since(0)
        review_req_events = [e for e in events if e.get("kind") == "human_review_required"]
        assert len(review_req_events) >= 1
        assert review_req_events[0].get("proposal_hash") == proposal_hash

        # Submit human decision via POST /api/run/accept
        status_accept, data_accept = _request(
            server, "POST", "/api/run/accept",
            body={"run_id": run_id, "proposal_hash": proposal_hash},
            token=launcher.token,
        )
        assert status_accept == 200, f"accept_proposal failed: {status_accept} - {data_accept}"
        accept_resp = json.loads(data_accept)
        assert accept_resp["accepted"] is True
        assert accept_resp["proposal_hash"] == proposal_hash

        # Wait for run to reach terminal completion
        for _ in range(60):
            with launcher._lock:
                if run_id in launcher._reports:
                    break
            time.sleep(0.1)

        assert run_id in launcher._reports, "Run did not complete after human decision"
        final_summary = launcher._reports[run_id]
        assert final_summary["accepted"] is True
        assert final_summary["held"] == 1
        assert final_summary["executed"] == 0
        assert final_summary["audit_persisted"] is True
        assert final_summary["audit_warning"] == ""

        # Verify approval_decided and run_completed events were emitted
        all_events = launcher.events.since(0)
        decided_events = [e for e in all_events if e.get("kind") == "approval_decided"]
        assert len(decided_events) >= 1
        assert decided_events[0].get("proposal_hash") == proposal_hash

        completed_events = [e for e in all_events if e.get("kind") == "run_completed"]
        assert len(completed_events) >= 1

        # Verify durable audit record on disk has exact proposal_hash and query_unit_id continuity
        failed_log = audit_dir / "failed" / "audit.jsonl"
        assert failed_log.exists(), "audit/failed/audit.jsonl was not created"
        records = [json.loads(l) for l in failed_log.read_text(encoding="utf-8").strip().splitlines()]
        matching_audits = [r for r in records if r.get("proposal_hash") == proposal_hash]
        assert len(matching_audits) == 1
        audit_rec = matching_audits[0]
        assert audit_rec["query_unit_id"] == query_unit_id
        assert audit_rec["proposal_action"] == action
        assert audit_rec["approval_approved"] is False
        assert audit_rec["execution_executed"] is False
        assert audit_rec["human_review_state"] == "acknowledged_non_executable"
        assert audit_rec["validation_result"] == "held_non_executable"

        # Verify that duplicate/stale decision is rejected deterministically
        status_dup, data_dup = _request(
            server, "POST", "/api/run/accept",
            body={"run_id": run_id, "proposal_hash": proposal_hash},
            token=launcher.token,
        )
        assert status_dup in (400, 409), f"Duplicate accept must be rejected with 400 or 409, got {status_dup}"

        status_approve_stale, data_approve_stale = _request(
            server, "POST", "/api/approve",
            body={"proposal_hash": proposal_hash, "query_unit_id": query_unit_id, "decision": "approve"},
            token=launcher.token,
        )
        assert status_approve_stale in (400, 409), f"Stale approve must be rejected with 400 or 409, got {status_approve_stale}"
    finally:
        server.shutdown()


def test_accept_proposal_requires_exact_proposal_identity():
    """Verify accept_proposal enforces exact proposal hash identity and run binding.

    1. Missing proposal_hash is rejected (400).
    2. Empty/whitespace proposal_hash is rejected (400).
    3. Malformed proposal_hash is rejected (400).
    4. Incorrect proposal_hash is rejected (400).
    5. Correct proposal_hash paired with an unrelated run_id is rejected (409).
    6. Non-mutating: Failed requests do not resolve or clear pending approval.
    7. CREATE proposals cannot be accepted via accept_proposal (400).
    8. Valid matching acknowledgment succeeds (200).
    9. Duplicate or stale acknowledgment is rejected deterministically (409).
    """
    from src.living_authenticity.adapter.app import _Pending, ApprovalState

    launcher, server = _server()
    try:
        run_id = "run-identity-test-1"
        valid_hash = "a" * 64
        mismatched_hash = "b" * 64

        # Register pending non-CREATE review
        pending = _Pending(
            run_id=run_id,
            key=valid_hash,
            payload={"proposal_action": "NEEDS_REVIEW", "query_unit_id": "qu-identity-1"},
        )
        with launcher._lock:
            launcher._pending_approval = pending

        # 1. Missing proposal_hash -> 400
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id}, token=launcher.token)
        assert st == 400, f"Missing hash must be 400, got {st}: {data}"
        assert "required" in json.loads(data)["error"]["message"].lower()
        # Verify state not mutated
        assert launcher._pending_approval is pending
        assert pending.state == ApprovalState.PENDING

        # 2. Empty proposal_hash -> 400
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": "   "}, token=launcher.token)
        assert st == 400, f"Empty hash must be 400, got {st}: {data}"
        assert launcher._pending_approval is pending

        # 3. Malformed proposal_hash -> 400
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": "not-a-valid-hex-hash"}, token=launcher.token)
        assert st == 400, f"Malformed hash must be 400, got {st}: {data}"
        assert launcher._pending_approval is pending

        # 4. Incorrect proposal_hash -> 400
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": mismatched_hash}, token=launcher.token)
        assert st == 400, f"Mismatched hash must be 400, got {st}: {data}"
        assert "does not match" in json.loads(data)["error"]["message"].lower()
        assert launcher._pending_approval is pending

        # 5. Correct proposal_hash with unrelated run_id -> 409
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": "unrelated-run-999", "proposal_hash": valid_hash}, token=launcher.token)
        assert st == 409, f"Unrelated run_id must be 409, got {st}: {data}"
        assert launcher._pending_approval is pending

        # 6. Verify CREATE proposals are rejected via accept_proposal -> 400
        create_hash = "c" * 64
        create_pending = _Pending(
            run_id=run_id,
            key=create_hash,
            payload={"proposal_action": "CREATE", "query_unit_id": "qu-create-1"},
        )
        with launcher._lock:
            launcher._pending_approval = create_pending

        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": create_hash}, token=launcher.token)
        assert st == 400, f"CREATE action on accept must be 400, got {st}: {data}"
        assert "explicit approval" in json.loads(data)["error"]["message"].lower()

        # Restore non-CREATE pending
        with launcher._lock:
            launcher._pending_approval = pending

        # 7. Valid matching acknowledgment -> 200
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": valid_hash}, token=launcher.token)
        assert st == 200, f"Valid accept must return 200, got {st}: {data}"
        resp = json.loads(data)
        assert resp["accepted"] is True
        assert resp["action"] == "NEEDS_REVIEW"
        assert resp["proposal_hash"] == valid_hash
        assert launcher._pending_approval is None
        assert pending.state == ApprovalState.ACKNOWLEDGED

        # 8. Duplicate acknowledgment -> 409
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": valid_hash}, token=launcher.token)
        assert st in (400, 409), f"Duplicate accept must be rejected with 400 or 409, got {st}: {data}"

        # 9. Acknowledgment when run has already reached terminal state -> 409
        with launcher._lock:
            launcher._reports[run_id] = {"accepted": True, "executed": 0, "held": 1}
        st, data = _request(server, "POST", "/api/run/accept", body={"run_id": run_id, "proposal_hash": valid_hash}, token=launcher.token)
        assert st == 409, f"Terminal run accept must be 409, got {st}: {data}"
    finally:
        server.shutdown()


def test_report_summary_accounts_for_every_expected_record_1_to_1():
    """Verify _report_summary enforces strict, bijective, unique accounting of audit records.

    1. Two expected records with two valid receipts -> audit_persisted=True.
    2. Two expected records with only one valid receipt -> audit_persisted=False.
    3. Two expected records with both valid receipts plus an additional duplicate receipt -> audit_persisted=False.
    4. Duplicate receipts for the same record while another expected record is missing a receipt -> audit_persisted=False.
    5. Unmatchable or missing record identity -> audit_persisted=False.
    6. Failed and unconfigured receipts -> audit_persisted=False.
    """
    from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
    from src.living_authenticity.knowledge.integration.outcome import IntegrationRunReport

    launcher = _launcher()

    rec1 = AuditRecord(
        record_id="rec-001",
        run_id="run-1",
        query_unit_id="qu-1",
        proposal_hash="a" * 64,
        operating_mode="examples",
        proposal_action="CREATE",
        terminal_outcome="executed",
    )
    rec2 = AuditRecord(
        record_id="rec-002",
        run_id="run-1",
        query_unit_id="qu-2",
        proposal_hash="b" * 64,
        operating_mode="examples",
        proposal_action="NEEDS_REVIEW",
        terminal_outcome="held_non_executable",
    )

    receipt1 = {
        "record_id": "rec-001",
        "persisted": True,
        "emergency": False,
        "mode": "primary",
        "durability": "written",
    }
    receipt2 = {
        "record_id": "rec-002",
        "persisted": True,
        "emergency": False,
        "mode": "primary",
        "durability": "written",
    }

    # Scenario 1: Two expected records with their two valid receipts
    rep_complete = IntegrationRunReport(
        accepted=True,
        files_processed=2,
        units=(),
        audits=(rec1, rec2),
        details=(),
        audit_receipts=(receipt1, receipt2),
    )
    summary_complete = launcher._report_summary(rep_complete)
    assert summary_complete["audit_persisted"] is True, "All records accounted 1-to-1 must report audit_persisted=True"
    assert summary_complete["audit_warning"] == ""

    # Scenario 2: Two expected records with only one valid receipt
    rep_partial = IntegrationRunReport(
        accepted=True,
        files_processed=2,
        units=(),
        audits=(rec1, rec2),
        details=(),
        audit_receipts=(receipt1,),
    )
    summary_partial = launcher._report_summary(rep_partial)
    assert summary_partial["audit_persisted"] is False, "Partial receipts must report audit_persisted=False"
    assert "missing durable persistence receipt" in summary_partial["audit_warning"]
    assert "1 expected audit record(s)" in summary_partial["audit_warning"]

    # Scenario 3: Two expected records with both valid receipts plus an additional duplicate receipt
    receipt1_duplicate = dict(receipt1)
    rep_surplus_dup = IntegrationRunReport(
        accepted=True,
        files_processed=2,
        units=(),
        audits=(rec1, rec2),
        details=(),
        audit_receipts=(receipt1, receipt2, receipt1_duplicate),
    )
    summary_surplus_dup = launcher._report_summary(rep_surplus_dup)
    assert summary_surplus_dup["audit_persisted"] is False, "Surplus duplicate receipt must report audit_persisted=False"
    assert "duplicate or surplus persistence receipt" in summary_surplus_dup["audit_warning"]

    # Scenario 4: Duplicate receipts for the same record while another expected record is missing a receipt
    rep_dup_with_missing = IntegrationRunReport(
        accepted=True,
        files_processed=2,
        units=(),
        audits=(rec1, rec2),
        details=(),
        audit_receipts=(receipt1, receipt1_duplicate),
    )
    summary_dup_with_missing = launcher._report_summary(rep_dup_with_missing)
    assert summary_dup_with_missing["audit_persisted"] is False, "Duplicate receipt cannot mask missing record"
    assert "missing durable persistence receipt" in summary_dup_with_missing["audit_warning"]
    assert "duplicate receipt" in summary_dup_with_missing["audit_warning"]

    # Scenario 5: Unmatchable or missing record identity
    # 5a: Missing record_id on expected record
    rec_missing_id = AuditRecord(
        record_id="",
        run_id="run-1",
        query_unit_id="qu-empty",
        proposal_hash="c" * 64,
        operating_mode="examples",
        proposal_action="NEEDS_REVIEW",
        terminal_outcome="held_non_executable",
    )
    rep_missing_rec_id = IntegrationRunReport(
        accepted=True,
        files_processed=1,
        units=(),
        audits=(rec_missing_id,),
        details=(),
        audit_receipts=(receipt1,),
    )
    summary_missing_rec_id = launcher._report_summary(rep_missing_rec_id)
    assert summary_missing_rec_id["audit_persisted"] is False
    assert "lacks a valid, unambiguous record identity" in summary_missing_rec_id["audit_warning"]

    # 5b: Missing record_id on receipt
    receipt_missing_id = {
        "record_id": "",
        "persisted": True,
        "emergency": False,
        "mode": "primary",
        "durability": "written",
    }
    rep_missing_receipt_id = IntegrationRunReport(
        accepted=True,
        files_processed=1,
        units=(),
        audits=(rec1,),
        details=(),
        audit_receipts=(receipt_missing_id,),
    )
    summary_missing_receipt_id = launcher._report_summary(rep_missing_receipt_id)
    assert summary_missing_receipt_id["audit_persisted"] is False
    assert "lacks a valid, unambiguous record identity" in summary_missing_receipt_id["audit_warning"]

    # 5c: Surplus receipt with unmatchable record_id
    receipt_unmatchable = {
        "record_id": "rec-unmatchable-999",
        "persisted": True,
        "emergency": False,
        "mode": "primary",
        "durability": "written",
    }
    rep_surplus_unmatchable = IntegrationRunReport(
        accepted=True,
        files_processed=1,
        units=(),
        audits=(rec1,),
        details=(),
        audit_receipts=(receipt1, receipt_unmatchable),
    )
    summary_surplus_unmatchable = launcher._report_summary(rep_surplus_unmatchable)
    assert summary_surplus_unmatchable["audit_persisted"] is False
    assert "surplus persistence receipt" in summary_surplus_unmatchable["audit_warning"]

    # Scenario 6: Failed and unconfigured receipts
    # 6a: Failed receipt
    failed_receipt2 = {
        "record_id": "rec-002",
        "persisted": False,
        "emergency": False,
        "mode": "primary",
        "durability": "failed",
        "reason": "disk quota exceeded",
    }
    rep_failed = IntegrationRunReport(
        accepted=True,
        files_processed=2,
        units=(),
        audits=(rec1, rec2),
        details=(),
        audit_receipts=(receipt1, failed_receipt2),
    )
    summary_failed = launcher._report_summary(rep_failed)
    assert summary_failed["audit_persisted"] is False
    assert "failed to persist to disk" in summary_failed["audit_warning"]

    # 6b: Unconfigured receipt
    unconfigured_receipt = {
        "record_id": "rec-001",
        "persisted": False,
        "emergency": False,
        "mode": "unconfigured",
        "durability": "unconfigured",
        "reason": "no audit writer configured",
    }
    rep_unconfigured = IntegrationRunReport(
        accepted=True,
        files_processed=1,
        units=(),
        audits=(rec1,),
        details=(),
        audit_receipts=(unconfigured_receipt,),
    )
    summary_unconfigured = launcher._report_summary(rep_unconfigured)
    assert summary_unconfigured["audit_persisted"] is False
    assert "Audit infrastructure is unconfigured" in summary_unconfigured["audit_warning"]
