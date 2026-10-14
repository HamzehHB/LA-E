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
