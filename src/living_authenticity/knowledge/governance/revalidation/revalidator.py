"""Deterministic revalidator: explicit checks, no execution."""
from src.living_authenticity.knowledge.governance.approval.outcome import ApprovalOutcome, ApprovalRequest
from src.living_authenticity.knowledge.governance.approval.proposal_hash import (
    canonical_proposal_hash,
    proposal_payload,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
from src.living_authenticity.knowledge.decision.proposal.outcome import PROPOSAL_ACTIONS, Proposal
from .outcome import REVALIDATION_CHECKS, RevalidationResult
from .schema_version import current_schema_version


def _fail(check, reason):
    return RevalidationResult(valid=False, failed_check=check, reason=reason,
        checks=REVALIDATION_CHECKS, approval_bound=False, eligible=False)

class Revalidator:
    """Revalidate one explicit approval against one live proposal.

    Pure validation only: no filesystem, vault, staging, shell, network,
    or approval and execution side effects.
    """

    def revalidate(self, request, outcome, proposal=None, confidence=None,
                   schema_version="", note=None):
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be an ApprovalRequest")
        if not isinstance(outcome, ApprovalOutcome):
            raise TypeError("outcome must be an ApprovalOutcome")
        if proposal is not None and not isinstance(proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        if confidence is not None and not isinstance(confidence, ConfidenceAssessment):
            raise TypeError("confidence must be a ConfidenceAssessment")
        if schema_version is not None and not isinstance(schema_version, str):
            raise TypeError("schema_version must be a string")
        current = current_schema_version()
        if schema_version and (not current or schema_version != current):
            return _fail("schema_state", "applicable schema/state changed after approval")
        if outcome.proposal_hash != request.proposal_hash:
            return _fail("approval_binding", "approval bound to a different proposal")
        if outcome.query_unit_id != request.query_unit_id:
            return _fail("target", "approval bound to a different query unit")
        if not outcome.approved:
            return _fail("approval_binding", "proposal was not approved")
        if request.proposal_action not in PROPOSAL_ACTIONS:
            return _fail("action_eligibility", "action outside executable scope")
        if proposal is None:
            return _fail("proposal_identity", "live proposal missing for revalidation")
        if proposal.query_unit_id != request.query_unit_id:
            return _fail("target", "proposed/actual target changed after approval")
        if proposal.query_source != request.query_source:
            return _fail("target", "proposed/actual target source changed after approval")
        if proposal.query_position != request.query_position:
            return _fail("target", "proposed/actual target position changed after approval")
        if proposal.destination != request.destination:
            return _fail("destination", "destination changed after approval")
        if proposal.action != request.proposal_action:
            return _fail("proposal_contents", "proposal contents changed after approval")
        if proposal.title != request.title:
            return _fail("proposal_contents", "proposal contents changed after approval")
        if proposal.reason != request.reason:
            return _fail("proposal_contents", "proposal contents changed after approval")
        live = proposal_payload(
            proposal, confidence,
            note if getattr(request, "note_bound", False) else None)
        if confidence is None:
            if request.confidence_level:
                return _fail("relevant_data", "confidence evidence missing for revalidation")
        elif live.get("confidence_level", "") != request.confidence_level:
            return _fail("relevant_data", "confidence evidence changed after approval")
        if canonical_proposal_hash(live) != request.proposal_hash:
            return _fail("proposal_identity", "proposal identity changed after approval")
        if not proposal.provenance:
            return _fail("provenance", "provenance missing or invalid")
        want = ("query:" + proposal.query_unit_id + "@" + proposal.query_source
            + "#" + str(proposal.query_position))
        if proposal.provenance[0] != want:
            return _fail("provenance", "provenance mismatch after approval")
        return RevalidationResult(valid=True, failed_check="",
            reason="revalidated: exact proposal unchanged",
            checks=REVALIDATION_CHECKS, approval_bound=True, eligible=True)


def revalidate_proposal(request, outcome, proposal=None, confidence=None,
                        schema_version=""):
    """Revalidate one approval against one live proposal (no execution)."""
    return Revalidator().revalidate(request, outcome, proposal, confidence, schema_version)
