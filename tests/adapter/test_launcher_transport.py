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
