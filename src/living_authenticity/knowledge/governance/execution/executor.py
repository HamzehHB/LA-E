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


import os
import uuid


class QuarantineOutcome(str):
    """Result of quarantine operation preserving string compatibility.

    If successful, evaluates to a non-empty string of the relative target path.
    If completely failed or partial cleanup failure, evaluates to empty string ("")
    so boolean checks (e.g. `if q_path:`) truthfully treat it as not successfully quarantined.
    Detailed attributes (.success, .status, .target_path, .source_removed, .error) provide
    exact filesystem state.
    """

    success: bool
    status: str  # "quarantined", "quarantine_failed", "partial_cleanup_failed"
    target_path: str
    source_removed: bool
    error: str

    def __new__(
        cls,
        val: str = "",
        success: bool = False,
        status: str = "quarantine_failed",
        target_path: str = "",
        source_removed: bool = False,
        error: str = "",
    ):
        obj = super().__new__(cls, val)
        obj.success = bool(success)
        obj.status = str(status)
        obj.target_path = str(target_path)
        obj.source_removed = bool(source_removed)
        obj.error = str(error)
        return obj


def atomic_publish_exclusive(tmp_path: Path, target_path: Path) -> None:
    """Atomically move tmp_path to target_path with strict no-overwrite guarantees.

    Raises FileExistsError if target_path exists prior to or during publication.
    Raises OSError if publication fails or source removal fails on link-based publication.
    """
    if os.name != "nt":
        try:
            os.link(str(tmp_path), str(target_path))
        except FileExistsError:
            raise
        except (AttributeError, NotImplementedError, OSError) as exc:
            if target_path.exists():
                raise FileExistsError(f"Target already exists: {target_path}") from exc
            raise OSError(
                f"Exclusive atomic publication primitive not supported or failed on POSIX: {exc}"
            ) from exc
        os.unlink(str(tmp_path))
        return

    try:
        os.link(str(tmp_path), str(target_path))
    except FileExistsError:
        raise
    except (AttributeError, NotImplementedError, OSError) as exc:
        if target_path.exists():
            raise FileExistsError(f"Target already exists: {target_path}") from exc
        os.rename(str(tmp_path), str(target_path))
        return
    os.unlink(str(tmp_path))


def _write_all(fd: int, data: bytes) -> None:
    """Write entire buffer to fd, handling partial writes and zero-progress."""
    total = len(data)
    written = 0
    while written < total:
        n = os.write(fd, data[written:])
        if n <= 0:
            raise OSError("no progress during staging write")
        written += n


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
        tmp = resolved.with_name(f".{resolved.name}.{uuid.uuid4().hex}.tmp")
        publish_exc: Exception | None = None
        fd: int | None = None
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            fd = os.open(str(tmp), flags, 0o600)
            payload = note.markdown.encode("utf-8")
            _write_all(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = None
            atomic_publish_exclusive(tmp, resolved)
        except FileExistsError:
            return _reject(request.proposal_hash, action, destination,
                           "staging destination exists; no overwrite",
                           "destination", provenance)
        except OSError as exc:
            publish_exc = exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

        # Evaluate final observed filesystem state after cleanup attempts have finished:
        if resolved.exists():
            if tmp.exists():
                return ExecutionResult(
                    attempted=True, permitted=True, executed=True,
                    proposal_hash=request.proposal_hash, action="CREATE",
                    destination=destination, artifact_path=resolved.name,
                    reason=f"executed with warning: published to staging but source temporary cleanup failed ({publish_exc or 'temporary artifact retained'})",
                    failed_check="", revalidation_valid=True, provenance=provenance,
                )
            return ExecutionResult(
                attempted=True, permitted=True, executed=True,
                proposal_hash=request.proposal_hash, action="CREATE",
                destination=destination, artifact_path=resolved.name,
                reason="executed: CREATE written to staging", failed_check="",
                revalidation_valid=True, provenance=provenance,
            )
        return _reject(request.proposal_hash, action, destination,
                       "staging write failed", "destination", provenance)

    def quarantine_artifact(self, staging_root: Path | str, artifact_path: str) -> str:
        """Quarantine an unreconciled artifact within staging root using atomic move."""
        return quarantine_artifact(staging_root, artifact_path)


def quarantine_artifact(staging_root: Path | str, artifact_path: str) -> QuarantineOutcome:
    """Quarantine an unreconciled artifact within staging root with atomic no-overwrite guarantees."""
    if not staging_root or not artifact_path:
        return QuarantineOutcome("", success=False, status="quarantine_failed", error="missing parameters")
    stg = Path(staging_root)
    source = stg / artifact_path
    try:
        boundary = PathBoundary(stg)
        resolved_source = boundary.validate(source)
    except Exception as exc:
        return QuarantineOutcome("", success=False, status="quarantine_failed", error=f"source invalid: {exc}")

    resolved_stg = stg.expanduser().resolve()
    if resolved_source == resolved_stg or not resolved_source.is_file():
        return QuarantineOutcome("", success=False, status="quarantine_failed", error="source is not a file")
    if resolved_stg not in resolved_source.parents:
        return QuarantineOutcome("", success=False, status="quarantine_failed", error="source outside staging")

    target_dir = stg / ".unreconciled"
    if target_dir.is_dir():
        target = target_dir / resolved_source.name
        rel_target = f".unreconciled/{resolved_source.name}"
    else:
        target = stg / f".unreconciled.{resolved_source.name}"
        rel_target = f".unreconciled.{resolved_source.name}"

    try:
        resolved_target = boundary.validate(target)
    except Exception as exc:
        return QuarantineOutcome("", success=False, status="quarantine_failed", error=f"target invalid: {exc}")

    if resolved_target.exists():
        return QuarantineOutcome("", success=False, status="quarantine_failed", target_path=rel_target, error="quarantine destination already exists")

    try:
        atomic_publish_exclusive(resolved_source, resolved_target)
        if resolved_target.exists() and not resolved_source.exists():
            return QuarantineOutcome(rel_target, success=True, status="quarantined", target_path=rel_target, source_removed=True)
        elif resolved_target.exists() and resolved_source.exists():
            return QuarantineOutcome("", success=False, status="partial_cleanup_failed", target_path=rel_target, source_removed=False, error="target created but source removal failed")
        return QuarantineOutcome("", success=False, status="quarantine_failed", target_path="", source_removed=False, error="quarantine destination missing")
    except FileExistsError as exc:
        return QuarantineOutcome("", success=False, status="quarantine_failed", target_path=rel_target, source_removed=False, error=f"destination exists: {exc}")
    except OSError as exc:
        if resolved_target.exists() and not resolved_source.exists():
            return QuarantineOutcome(rel_target, success=True, status="quarantined", target_path=rel_target, source_removed=True)
        if resolved_target.exists() and resolved_source.exists():
            return QuarantineOutcome("", success=False, status="partial_cleanup_failed", target_path=rel_target, source_removed=False, error=f"target created but source removal failed: {exc}")
        return QuarantineOutcome("", success=False, status="quarantine_failed", target_path="", source_removed=False, error=f"quarantine failed: {exc}")


def execute_create(request, outcome, proposal=None, confidence=None,
                   note=None, staging_root=None, schema_version=""):
    """Execute one approved CREATE proposal into the staging root."""
    return ControlledExecutor().execute(
        request, outcome, proposal, confidence, note, staging_root,
        schema_version or "")

