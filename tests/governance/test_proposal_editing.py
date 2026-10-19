"""Server-side proposal editing: integrity + approval-invalidation proof."""
from src.living_authenticity.knowledge.governance.approval.editing import (
    ProposalEditError,
    apply_proposal_edit,
)
from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
    ExplicitApprovalGate,
)
from src.living_authenticity.knowledge.governance.approval.proposal_hash import (
    canonical_proposal_hash,
    proposal_payload,
)
from src.living_authenticity.knowledge.governance.execution import execute_create
from src.living_authenticity.knowledge.governance.revalidation import (
    Revalidator,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.output.evidence_note import (
    DefaultOutputGenerator,
)
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal


def _query():
    return KnowledgeUnit(id="u1", source="src", original_text="hello",
                         meaning="hello", position=0)


def _proposal(**over):
    base = {"query_unit_id": "u1", "query_source": "src",
            "query_position": 0, "action": "CREATE", "title": "Title One",
            "destination": "Inbox suggestion",
            "reason": "evidence supports a new draft",
            "provenance": ("query:u1@src#0",)}
    base.update(over)
    return Proposal(**base)


def _make_note(query, proposal, body):
    gen = DefaultOutputGenerator()
    note = gen.generate(query, proposal=proposal)
    import dataclasses
    return dataclasses.replace(note, markdown=body, body=body)


def test_edit_recomputes_hash_server_side_and_ignores_client_hash():
    query = _query()
    proposal = _proposal()
    note = _make_note(query, proposal, "Original body.")
    edited = apply_proposal_edit(
        proposal, note, title="Edited Title", body="Edited body text.",
        client_hash="attacker-supplied-hash")
    # Authoritative hash is recomputed, never the client value.
    assert edited.proposal_hash != "attacker-supplied-hash"
    assert edited.proposal_hash == canonical_proposal_hash(
        proposal_payload(edited.proposal, None, edited.note))
    assert edited.changed is True
    assert edited.proposal.title == "Edited Title"
    assert edited.note.markdown == "Edited body text."


def test_edit_invalidates_prior_approval_and_requires_fresh_approval(tmp_path):
    query = _query()
    proposal = _proposal()
    note = _make_note(query, proposal, "Original body.")
    gate = ExplicitApprovalGate()

    # Human approves the ORIGINAL proposal (note bound into the hash).
    request = gate.build_request(query, proposal, None, note)
    outcome = gate.decide(request, "y")
    assert outcome.approved is True
    assert Revalidator().revalidate(
        request, outcome, proposal, None, "", note).valid is True

    # Now the proposal is edited -> new authoritative hash.
    edited = apply_proposal_edit(proposal, note, title="Edited Title",
                                 body="Edited body text.")

    # The OLD approval no longer authorizes the edited proposal.
    stale = Revalidator().revalidate(
        request, outcome, edited.proposal, None, "", edited.note)
    assert stale.valid is False
    assert stale.failed_check in ("proposal_contents", "proposal_identity")

    # A FRESH approval of the edited proposal is required and sufficient.
    new_request = gate.build_request(query, edited.proposal, None, edited.note)
    assert new_request.proposal_hash == edited.proposal_hash
    new_outcome = gate.decide(new_request, "y")
    fresh = Revalidator().revalidate(
        new_request, new_outcome, edited.proposal, None, "", edited.note)
    assert fresh.valid is True
    # And only the fresh approval may execute the edited content.
    execution = execute_create(new_request, new_outcome, edited.proposal,
                               None, edited.note, tmp_path)
    assert execution.executed is True


def test_non_create_proposals_are_not_editable_for_execution():
    query = _query()
    for action in ("NEEDS_REVIEW", "DO_NOT_IMPORT"):
        proposal = _proposal(action=action)
        note = _make_note(query, proposal, "Body.")
        try:
            apply_proposal_edit(proposal, note, title="X", body="Y")
        except ProposalEditError:
            pass
        else:
            raise AssertionError(action + " must not be editable")


def test_empty_or_invalid_edits_are_rejected():
    query = _query()
    proposal = _proposal()
    note = _make_note(query, proposal, "Body.")
    for kwargs in ({"body": "   "}, {"title": ""}, {"body": ""}):
        try:
            apply_proposal_edit(proposal, note, **kwargs)
        except ProposalEditError:
            continue
        raise AssertionError("invalid edit accepted: " + repr(kwargs))


def test_proposal_edit_end_to_end_adoption_lifecycle(tmp_path):
    """End-to-end integration proof for editable proposal lifecycle:

    1. Initial proposal hash H0 is created.
    2. Human edit changes hash to H1, adding 'supersedes:H0' to provenance.
    3. Approval of H1 succeeds through _govern_candidate.
    4. validator.revalidate succeeds against H1 and edited proposal/note.
    5. ControlledExecutor writes edited note markdown to staging artifact.
    6. Audit record links H1 and 'supersedes:H0' to the staging artifact.
    7. Unauthorized modifications fail revalidation.
    """
    import hashlib
    from src.living_authenticity.adapter.app import Launcher, _PublishingGate
    from src.living_authenticity.knowledge.governance.audit import AuditWriter
    from src.living_authenticity.knowledge.governance.execution import ControlledExecutor
    from src.living_authenticity.knowledge.governance.revalidation import Revalidator
    from src.living_authenticity.knowledge.integration.unit_loop import _govern_candidate

    staging = tmp_path / "staging"
    audit_root = tmp_path / "audit"
    staging.mkdir()
    audit_root.mkdir()

    launcher = Launcher(
        paths={
            "audit": {"root": str(audit_root), "passed": str(audit_root / "passed"), "failed": str(audit_root / "failed")},
            "staging": {"root": str(staging)},
        },
        llm_config={"llm": {"provider": "disabled"}},
        open_browser=False,
    )
    gate = _PublishingGate(launcher)

    query = _query()
    initial_proposal = _proposal()
    initial_note = _make_note(query, initial_proposal, "Initial generated markdown body.")

    # Calculate initial hash H0
    h0 = canonical_proposal_hash(proposal_payload(initial_proposal, None, initial_note))

    iteration = 0
    captured_h0 = None
    captured_h1 = None

    def _supervised_reader():
        nonlocal iteration, captured_h0, captured_h1
        iteration += 1
        if iteration == 1:
            pending = launcher._pending_approval
            assert pending is not None
            captured_h0 = pending.key
            assert captured_h0 == h0

            # Human edits the proposal while approval is pending
            edit_result = launcher.edit_proposal(
                title="Supervised Edited Title",
                body="Approved edited note markdown text.",
                reason="Edited by supervisor for exactness.",
            )
            captured_h1 = edit_result["proposal_hash"]
            assert captured_h1 != captured_h0
            assert edit_result["supersedes"] == captured_h0
            return "y"
        else:
            pending = launcher._pending_approval
            assert pending is not None
            assert pending.key == captured_h1
            return "y"

    entries = []
    audits = []

    _govern_candidate(
        "child.md", query, initial_proposal, None, initial_note,
        gate, Revalidator(), ControlledExecutor(),
        str(staging), (), "", _supervised_reader, entries, audits,
        authorized_staging="",
        audit_writer=AuditWriter(str(audit_root)),
    )

    # 1. H0 was superseded and 2. H1 was created
    assert captured_h0 == h0
    assert captured_h1 is not None and captured_h1 != h0

    # 3. Approval of H1 succeeded through _govern_candidate
    assert len(entries) == 1
    entry = entries[0]
    assert entry.outcome == "executed"
    assert entry.approval_approved is True
    assert entry.proposal_hash == captured_h1
    assert entry.note_markdown == "Approved edited note markdown text."

    # 4. validator.revalidate succeeded
    assert entry.revalidation_valid is True

    # 5. ControlledExecutor wrote the edited markdown to staging
    staged_file = staging / f"{captured_h1}.md"
    assert staged_file.is_file(), f"Expected artifact {captured_h1}.md in staging"
    written_text = staged_file.read_text(encoding="utf-8")
    assert written_text == "Approved edited note markdown text."

    # 6. Audit record links H1 and supersedes:H0 to the staging artifact
    post_exec_audits = [a for a in audits if getattr(a, "audit_phase", "") == "post_execution"]
    assert len(post_exec_audits) == 1
    audit = post_exec_audits[0]
    assert audit.proposal_hash == captured_h1
    assert audit.execution_executed is True
    assert audit.artifact_reference == f"{captured_h1}.md"
    expected_artifact_sha = hashlib.sha256("Approved edited note markdown text.".encode("utf-8")).hexdigest()
    assert audit.artifact_sha256 == expected_artifact_sha
    assert f"supersedes:{captured_h0}" in audit.provenance

    # 7. Unauthorized modifications fail revalidation
    validator = Revalidator()
    gate_req = gate.build_request(query, gate.last_active_proposal, None, gate.last_active_note)
    gate_out = gate.decide(gate_req, "y")

    # Mismatch proposal hash with wrong proposal
    tampered_proposal = _proposal(title="Tampered Title Without Re-approval")
    tampered_check = validator.revalidate(gate_req, gate_out, tampered_proposal, None, "", gate.last_active_note)
    assert tampered_check.valid is False
    assert tampered_check.failed_check in ("proposal_contents", "proposal_identity")

    # Tampered note content fails revalidation closed
    tampered_note = _make_note(query, gate.last_active_proposal, "Tampered markdown unapproved.")
    tampered_note_check = validator.revalidate(gate_req, gate_out, gate.last_active_proposal, None, "", tampered_note)
    assert tampered_note_check.valid is False
    assert tampered_note_check.failed_check in ("proposal_contents", "proposal_identity")


def test_govern_candidate_fail_fast_on_invalid_or_missing_note(tmp_path):
    """Verify that _govern_candidate strictly rejects None or invalid GeneratedNote."""
    import pytest
    from src.living_authenticity.knowledge.governance.execution import ControlledExecutor
    from src.living_authenticity.knowledge.governance.revalidation import Revalidator
    from src.living_authenticity.knowledge.integration.unit_loop import _govern_candidate

    staging = tmp_path / "staging"
    staging.mkdir()
    gate = ExplicitApprovalGate()
    query = _query()
    proposal = _proposal()

    # 1. note is None -> TypeError
    with pytest.raises(TypeError, match="note must be a GeneratedNote"):
        _govern_candidate(
            "child.md", query, proposal, None, None,
            gate, Revalidator(), ControlledExecutor(),
            str(staging), (), "", lambda: "y", [], [],
        )

    # 2. note is wrong type -> TypeError
    with pytest.raises(TypeError, match="note must be a GeneratedNote"):
        _govern_candidate(
            "child.md", query, proposal, None, "plain string not GeneratedNote",
            gate, Revalidator(), ControlledExecutor(),
            str(staging), (), "", lambda: "y", [], [],
        )

    # 3. note.markdown is empty or whitespace -> ValueError
    empty_note = _make_note(query, proposal, "   ")
    with pytest.raises(ValueError, match="note.markdown must not be empty"):
        _govern_candidate(
            "child.md", query, proposal, None, empty_note,
            gate, Revalidator(), ControlledExecutor(),
            str(staging), (), "", lambda: "y", [], [],
        )


def test_launcher_edit_proposal_unblocks_approval_reader_thread(tmp_path):
    """Verify that editing unblocks a background thread waiting in _approval_reader."""
    import threading
    import time
    from src.living_authenticity.adapter.app import Launcher, _PublishingGate

    audit_dir = tmp_path / "audit"
    staging_dir = tmp_path / "staging"
    audit_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    launcher = Launcher(
        paths={
            "audit": {"root": str(audit_dir), "passed": str(audit_dir / "passed"), "failed": str(audit_dir / "failed")},
            "staging": {"root": str(staging_dir)},
        },
        llm_config={"llm": {"provider": "disabled"}},
        open_browser=False,
    )
    gate = _PublishingGate(launcher)
    query = _query()
    initial_proposal = _proposal()
    initial_note = _make_note(query, initial_proposal, "Initial markdown.")

    h0 = canonical_proposal_hash(proposal_payload(initial_proposal, None, initial_note))

    result_holder = {}
    thread_error = []

    def _worker():
        try:
            res = gate.request_approval(
                query, initial_proposal, None,
                reader=launcher._approval_reader,
                note=initial_note,
            )
            result_holder["result"] = res
        except Exception as exc:
            thread_error.append(exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    # Wait for the thread to register initial pending approval
    for _ in range(50):
        if launcher._pending_approval is not None:
            break
        time.sleep(0.05)

    assert launcher._pending_approval is not None
    assert launcher._pending_approval.key == h0

    # Main thread edits the proposal while approval is pending on background thread
    edit_res = launcher.edit_proposal(
        title="Thread Edited Title",
        body="Thread edited body text.",
    )
    h1 = edit_res["proposal_hash"]
    assert h1 != h0

    # Wait for the worker thread to unblock from H0 and re-register approval for H1
    for _ in range(50):
        if launcher._pending_approval is not None and launcher._pending_approval.key == h1:
            break
        time.sleep(0.05)

    assert launcher._pending_approval is not None
    assert launcher._pending_approval.key == h1

    # Main thread approves H1
    launcher.approve(h1, query.id, "approve")

    t.join(timeout=5.0)
    assert not t.is_alive(), "Worker thread failed to complete after approval"
    assert not thread_error, f"Worker thread encountered error: {thread_error}"

    req, text, out = result_holder["result"]
    assert req.proposal_hash == h1
    assert out.approved is True
    active_proposal = gate.last_active_proposal
    active_note = gate.last_active_note
    assert active_proposal is not None
    assert active_note is not None
    assert active_proposal.title == "Thread Edited Title"
    assert active_note.markdown == "Thread edited body text."
