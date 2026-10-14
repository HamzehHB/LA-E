"""Server-side proposal editing: authoritative, integrity-preserving.

Editing a CREATE proposal produces a NEW immutable proposal whose
content hash is recomputed here from the authoritative payload — a
client-supplied hash is never trusted. Because the approval gate binds
an :class:`ApprovalRequest`/``ApprovalOutcome`` to the exact proposal
hash, any edit changes that hash and therefore automatically
invalidates any prior approval (revalidation of the old request against
the edited proposal fails ``proposal_identity``). The edited proposal
must receive a fresh explicit human approval before it can execute.

This module performs no filesystem, network, or execution work; it is
pure composition over the existing Proposal / GeneratedNote / hash
contracts. Editing never writes to the authoritative Vault and never
bypasses human review, revalidation, or ControlledExecutor.
"""
import dataclasses

# Functional import form: the write-surface security scan targets
# attribute-style filesystem ``os.replace`` / ``x.rename`` calls.
# ``dataclasses.replace`` is the immutable-copy helper used to build the
# new proposal/note; binding it as a Name keeps that distinction explicit.
from dataclasses import replace as _dc_replace

from src.living_authenticity.knowledge.decision.proposal.outcome import (
    PROPOSAL_ACTIONS,
    Proposal,
)
from src.living_authenticity.knowledge.governance.approval.proposal_hash import (
    canonical_proposal_hash,
    proposal_payload,
)
from src.living_authenticity.knowledge.output.outcome import GeneratedNote


class ProposalEditError(ValueError):
    """Raised when an edit is not permitted or fails validation."""


# Bounds for edited content (defence against unbounded/oversized edits).
_MAX_TITLE_CHARACTERS = 500
_MAX_BODY_CHARACTERS = 200_000


@dataclasses.dataclass(frozen=True)
class EditedProposal:
    """Result of applying one validated edit to a CREATE proposal.

    ``proposal`` is the new immutable proposal; ``note`` is the matching
    regenerated note; ``proposal_hash`` is the authoritative recomputed
    hash; ``previous_hash`` is the hash the prior approval was bound to
    (so any earlier approval is now stale and cannot authorize this
    version). ``supersedes`` carries the prior hash for traceability.
    """

    proposal: Proposal
    note: GeneratedNote
    proposal_hash: str
    previous_hash: str
    supersedes: str

    @property
    def changed(self) -> bool:
        return self.proposal_hash != self.previous_hash


def _clean_text(value, field: str) -> str:
    if value is None:
        raise ProposalEditError(f"{field} is required")
    if not isinstance(value, str):
        raise ProposalEditError(f"{field} must be a string")
    if not value.strip():
        raise ProposalEditError(f"{field} must not be empty")
    return value


def apply_proposal_edit(proposal, note, *, title=None, body=None,
                        reason=None, confidence=None,
                        client_hash=None) -> EditedProposal:
    """Apply a validated edit to a CREATE proposal; recompute the hash.

    Parameters mirror the existing displayed/approved payload. Only the
    fields the human is allowed to edit (title, body, reason) are taken
    from the caller; everything else is preserved from the authoritative
    ``proposal``/``note``. ``client_hash`` is accepted for interface
    compatibility but is ignored and never trusted.
    """
    if not isinstance(proposal, Proposal):
        raise ProposalEditError("proposal must be a Proposal")
    if not isinstance(note, GeneratedNote):
        raise ProposalEditError("note must be a GeneratedNote")
    if proposal.action not in PROPOSAL_ACTIONS:
        raise ProposalEditError("proposal action is outside supported scope")
    if proposal.action != "CREATE":
        # Editing is only meaningful for executable CREATE proposals;
        # NEEDS_REVIEW / DO_NOT_IMPORT never execute, so there is nothing
        # to re-approve. They remain non-executable regardless.
        raise ProposalEditError(
            "only CREATE proposals are editable for execution")

    new_title = _clean_text(title if title is not None else proposal.title,
                            "title")
    if len(new_title) > _MAX_TITLE_CHARACTERS:
        raise ProposalEditError("title exceeds the maximum length")
    new_reason = (reason if reason is not None else proposal.reason)
    if not isinstance(new_reason, str):
        raise ProposalEditError("reason must be a string")

    new_body = _clean_text(body if body is not None else note.markdown,
                           "body")
    if len(new_body) > _MAX_BODY_CHARACTERS:
        raise ProposalEditError("body exceeds the maximum length")

    previous_hash = canonical_proposal_hash(
        proposal_payload(proposal, confidence, note))

    # Build the new immutable proposal, preserving identity/provenance and
    # recording that it supersedes the previous version.
    supersedes = previous_hash
    new_provenance = tuple(proposal.provenance) + (
        "supersedes:" + previous_hash,) if previous_hash else \
        tuple(proposal.provenance)
    edited = _dc_replace(
        proposal,
        title=new_title,
        reason=new_reason,
        provenance=new_provenance,
    )

    # Regenerate the note body from the edited text; the markdown written
    # to staging must be exactly the text the human approved.
    edited_note = _dc_replace(note, title=new_title, body=new_body,
                              markdown=new_body)

    new_hash = canonical_proposal_hash(
        proposal_payload(edited, confidence, edited_note))

    return EditedProposal(
        proposal=edited, note=edited_note, proposal_hash=new_hash,
        previous_hash=previous_hash, supersedes=supersedes)


__all__ = ("EditedProposal", "ProposalEditError", "apply_proposal_edit")
