"""Controlled single-proposal CREATE executor (staging confined)."""
from pathlib import Path

from src.living_authenticity.knowledge.governance.approval.outcome import (
    ApprovalOutcome,
    ApprovalRequest,
)
from src.living_authenticity.knowledge.decision.confidence.outcome import ConfidenceAssessment
from src.living_authenticity.knowledge.output.outcome import GeneratedNote
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal
from src.living_authenticity.knowledge.governance.revalidation.revalidator import Revalidator
from src.living_authenticity.security.path_boundary import (
    PathBoundary,
    PathOutsideBoundaryError,
)

from .outcome import ExecutionResult


def _reject(proposal_hash, action, destination, reason, failed_check,
            provenance=()):
    return ExecutionResult(
        attempted=True, permitted=False, executed=False,
        proposal_hash=proposal_hash, action=action, destination=destination,
        artifact_path="", reason=reason, failed_check=failed_check,
        revalidation_valid=False, provenance=tuple(provenance),
    )


class ControlledExecutor:
    """Execute one approved CREATE proposal into one staging root."""

    def execute(self, request, outcome, proposal=None, confidence=None,
                note=None, staging_root=None, schema_version=""):
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be an ApprovalRequest")
        if not isinstance(outcome, ApprovalOutcome):
            raise TypeError("outcome must be an ApprovalOutcome")
        if not isinstance(proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        if confidence is not None and not isinstance(
                confidence, ConfidenceAssessment):
            raise TypeError("confidence must be a ConfidenceAssessment")
        if not isinstance(note, GeneratedNote):
            raise TypeError("note must be a GeneratedNote")
        if schema_version is not None and not isinstance(schema_version, str):
            raise TypeError("schema_version must be a string")
        action = request.proposal_action or ""
        destination = request.destination or ""
        provenance = tuple(proposal.provenance)
        if not outcome.approved:
            return _reject(request.proposal_hash, action, destination,
                           "proposal was not approved", "approval_binding",
                           provenance)
        if outcome.proposal_hash != request.proposal_hash:
            return _reject(request.proposal_hash, action, destination,
                           "approval bound to a different proposal",
                           "approval_binding", provenance)
        if outcome.query_unit_id != request.query_unit_id:
            return _reject(request.proposal_hash, action, destination,
                           "approval bound to a different query unit",
                           "approval_binding", provenance)
        if request.proposal_action != "CREATE" or proposal.action != "CREATE":
            return _reject(request.proposal_hash, action, destination,
                           "action outside executable scope",
                           "action_eligibility", provenance)
        if note.query_unit_id != request.query_unit_id:
            return _reject(request.proposal_hash, action, destination,
                           "generated note bound to a different query unit",
                           "proposal_identity", provenance)
        if note.proposal_action != "CREATE":
            return _reject(request.proposal_hash, action, destination,
                           "generated note action mismatch",
                           "proposal_contents", provenance)
        if not note.markdown:
            return _reject(request.proposal_hash, action, destination,
                           "generated note content missing",
                           "proposal_contents", provenance)
        if staging_root is None or (isinstance(staging_root, str)
                                    and not staging_root.strip()):
            return _reject(request.proposal_hash, action, destination,
                           "staging root missing", "destination", provenance)
        try:
            boundary = PathBoundary(staging_root)
        except Exception:
            return _reject(request.proposal_hash, action, destination,
                           "staging root invalid", "destination", provenance)
        checked = Revalidator().revalidate(
            request, outcome, proposal, confidence, schema_version or "",
            note)
        if not checked.valid:
            return _reject(request.proposal_hash, action, destination,
                           checked.reason or "revalidation failed",
                           checked.failed_check or "proposal_identity",
                           provenance)
        target = Path(staging_root) / (request.proposal_hash + ".md")
        try:
            resolved = boundary.validate(target)
        except PathOutsideBoundaryError:
            return _reject(request.proposal_hash, action, destination,
                           "staging destination outside boundary",
                           "destination", provenance)
        except Exception:
            return _reject(request.proposal_hash, action, destination,
                           "staging destination invalid",
                           "destination", provenance)
        if resolved.exists():
            return _reject(request.proposal_hash, action, destination,
                           "staging destination exists; no overwrite",
                           "destination", provenance)
        try:
            resolved.write_text(note.markdown, encoding="utf-8")
        except OSError:
            return _reject(request.proposal_hash, action, destination,
                           "staging write failed", "destination", provenance)
        return ExecutionResult(
            attempted=True, permitted=True, executed=True,
            proposal_hash=request.proposal_hash, action="CREATE",
            destination=destination, artifact_path=resolved.name,
            reason="executed: CREATE written to staging", failed_check="",
            revalidation_valid=True, provenance=provenance,
        )


def execute_create(request, outcome, proposal=None, confidence=None,
                   note=None, staging_root=None, schema_version=""):
    """Execute one approved CREATE proposal into the staging root."""
    return ControlledExecutor().execute(
        request, outcome, proposal, confidence, note, staging_root,
        schema_version or "")

