"""Deterministic explicit-approval gate (analytical only).

Builds one ApprovalRequest per proposal, displays it, reads exactly one
human input via an injected callable, and records one ApprovalOutcome
bound to that proposal's hash. No batch, no standing, no transfer, no
fabrication, no authorization, no execution.
"""
from datetime import datetime, timezone

from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit
from src.living_authenticity.knowledge.decision.proposal.outcome import Proposal

from .approval_input import parse_approval_input
from .base_gate import ApprovalGate
from .outcome import ApprovalOutcome, ApprovalRequest
from .proposal_hash import canonical_proposal_hash, proposal_payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExplicitApprovalGate(ApprovalGate):
    """Request and record exactly one explicit human decision."""

    def __init__(self, strategy: str = "explicit_cli") -> None:
        self._strategy = strategy

    @property
    def name(self) -> str:
        return self._strategy

    def build_request(self, query, proposal=None, confidence=None,
                      note=None) -> ApprovalRequest:
        if not isinstance(query, KnowledgeUnit):
            raise TypeError("query must be a KnowledgeUnit")
        if not isinstance(proposal, Proposal):
            raise TypeError("proposal must be a Proposal")
        if proposal.query_unit_id != query.id:
            raise ValueError("proposal query_unit_id mismatch")
        payload = proposal_payload(proposal, confidence, note)
        digest = canonical_proposal_hash(payload)
        return ApprovalRequest(
            query_unit_id=query.id,
            query_source=query.source,
            query_position=query.position,
            proposal_action=proposal.action,
            title=proposal.title,
            destination=proposal.destination,
            reason=proposal.reason,
            confidence_level=payload.get("confidence_level", ""),
            proposal_hash=digest,
            strategy=self._strategy,
            note_bound=note is not None,
            note=(
                "Non-authoritative approval request for human review only; "
                "not authorization or execution. Destination is inert data."
            ),
            is_authoritative=False,
            requires_human_review=True,
        )

    def display(self, request: ApprovalRequest) -> str:
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be an ApprovalRequest")
        return (
            "Proposed Action: " + request.proposal_action + "\n"
            "Title:\n" + request.title + "\n"
            "Destination:\n" + request.destination + "\n"
            "Reason:\n" + request.reason + "\n"
            "Confidence:\n" + request.confidence_level + "\n"
            "Proposal hash:\n" + request.proposal_hash + "\n"
            "Approve this action? [y/N]:"
        )

    def decide(self, request, raw_input, timestamp: str = "") -> ApprovalOutcome:
        if not isinstance(request, ApprovalRequest):
            raise TypeError("request must be an ApprovalRequest")
        approved = parse_approval_input(raw_input)
        stamp = timestamp if isinstance(timestamp, str) and timestamp else _utc_now()
        return ApprovalOutcome(
            query_unit_id=request.query_unit_id,
            proposal_hash=request.proposal_hash,
            proposal_action=request.proposal_action,
            approved=approved,
            input_value=raw_input if isinstance(raw_input, str) else "",
            reason=("explicit human approval" if approved else "rejected by default"),
            timestamp=stamp,
            strategy=self._strategy,
            note=(
                "Single-proposal human decision record; "
                "not authorization or execution."
            ),
            is_authoritative=False,
            requires_human_review=True,
        )

    def request_approval(self, query, proposal=None, confidence=None,
                         reader=None, note=None) -> tuple:
        """Display one request, read one input, record one outcome.

        ``reader`` is an injected zero-argument callable returning the
        human's raw string (default ``input``). It is invoked at most
        once. Any exception (EOF, timeout, interruption) is rejection,
        never approval. ``note`` binds the displayed generated-note
        markdown into the canonical approval hash.
        """
        request = self.build_request(query, proposal, confidence, note)
        text = self.display(request)
        read = reader if reader is not None else input
        if not callable(read):
            raise TypeError("reader must be callable")
        try:
            raw = read()
        except Exception:
            raw = ""
        outcome = self.decide(request, raw)
        return (request, text, outcome)


class DefaultApprovalGate(ExplicitApprovalGate):
    """Default gate alias preserving explicit single-proposal behavior."""
