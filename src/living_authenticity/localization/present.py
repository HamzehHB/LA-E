"""Journal translation/presentation: derived, traceable, source-preserving."""
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from . import bidi_core as bidi
from .config import normalize_language
from .resources import get_message

MACHINE_AUDIT_FIELDS = (
    "record_id", "run_id", "query_unit_id", "proposal_hash",
    "approval_proposal_hash", "approval_query_unit_id",
    "execution_proposal_hash", "artifact_sha256", "artifact_reference",
    "artifact_path", "destination", "input_hash", "evidence_hash",
    "prompt_sha256", "llm_config_hash", "synthesis_result_id",
    "llm_provider", "llm_model", "timestamp", "execution_timestamp",
    "artifact_created_at", "approval_timestamp", "proposal_action",
    "operating_mode", "audit_phase",
)

MACHINE_STAGING_FIELDS = (
    "query_unit_id", "query_source", "proposal_action", "destination",
    "proposed_type", "confidence_level", "strategy",
)


class TranslationStatus:
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


def _sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


@dataclass(frozen=True)
class TranslationRecord:
    """One derived translated representation bound to its source."""

    source_kind: str = ""
    source_id: str = ""
    source_language: str = "en"
    target_language: str = "en"
    status: str = "current"
    source_hash: str = ""
    translated_hash: str = ""
    translated_text: str = ""
    translator: str = "static-resources-v1"
    translated_at: str = ""
    version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_language",
                           normalize_language(self.source_language))
        object.__setattr__(self, "target_language",
                           normalize_language(self.target_language))
        valid = ("current", "stale", "missing", "invalid", "unavailable")
        if self.status not in valid:
            raise ValueError("invalid translation status")


def build_translation_record(source_kind: str, source_id: str,
                             source_text: str, source_language: str,
                             target_language: str,
                             translated_text: str,
                             translator: str = "static-resources-v1",
                             status: str = "current") -> "TranslationRecord":
    """Build a traceable translation record (pure, no I/O)."""
    src = normalize_language(source_language)
    tgt = normalize_language(target_language)
    return TranslationRecord(
        source_kind=source_kind, source_id=source_id or "",
        source_language=src, target_language=tgt, status=status,
        source_hash=_sha256(source_text or ""),
        translated_hash=_sha256(translated_text or ""),
        translated_text=translated_text or "",
        translator=translator or "static-resources-v1",
        translated_at=_now_iso())


def is_translation_current(record: "TranslationRecord",
                           current_source_text: str) -> bool:
    """True when record matches current source text and is current."""
    if not isinstance(record, TranslationRecord):
        return False
    if record.status != TranslationStatus.CURRENT:
        return False
    return record.source_hash == _sha256(current_source_text or "")


def translate_journal_entry(source_kind: str, source_id: str,
                            source_text: str, source_language: str,
                            target_language: str,
                            translator=None,
                            translator_name: str = "") -> "TranslationRecord":
    """Translate one journal text; never touches the source entry."""
    src = normalize_language(source_language)
    tgt = normalize_language(target_language)
    if src == tgt:
        return build_translation_record(source_kind, source_id, source_text,
                                        src, tgt, source_text or "",
                                        translator_name or "identity",
                                        TranslationStatus.CURRENT)
    rendered = ""
    name = translator_name or "custom-translator"
    if callable(translator):
        try:
            rendered = translator(source_text or "", src, tgt) or ""
        except Exception:
            rendered = ""
    if not (isinstance(rendered, str) and rendered.strip()):
        return build_translation_record(
            source_kind, source_id, source_text, src, tgt,
            source_text or "", name, TranslationStatus.UNAVAILABLE)
    return build_translation_record(source_kind, source_id, source_text,
                                    src, tgt, rendered, name,
                                    TranslationStatus.CURRENT)


def present_staging_note(note, language: str,
                         translator=None) -> "TranslationRecord":
    """Present a staging note's human text in ``language`` (source kept)."""
    code = normalize_language(language)
    title = getattr(note, "title", "") or ""
    body = getattr(note, "body", "") or ""
    source_text = ("# " + title + "\n\n" + body).strip()
    source_id = getattr(note, "query_unit_id", "") or ""
    banner = get_message("staging.review_banner", code)
    if code == "fa":
        banner = bidi.wrap_rtl(banner)
        shown = bidi.isolate_machine_segments(source_text) if source_text else ""
    else:
        shown = source_text
    rendered = (banner + "\n\n" + shown).strip()
    if code == "en":
        return build_translation_record("staging", source_id, source_text,
                                        "en", "en", rendered,
                                        "static-resources-v1")
    if callable(translator):
        return translate_journal_entry("staging", source_id, rendered,
                                       "en", code, translator,
                                       "custom-translator")
    return build_translation_record("staging", source_id, source_text,
                                    "en", code, rendered,
                                    "static-resources-v1",
                                    TranslationStatus.UNAVAILABLE)


def present_audit_record(record, language: str) -> dict:
    """Return a presentation view; machine fields copied verbatim."""
    from dataclasses import asdict, is_dataclass
    code = normalize_language(language)
    if is_dataclass(record) and not isinstance(record, type):
        machine = asdict(record)
    elif isinstance(record, dict):
        machine = dict(record)
    else:
        raise TypeError("record must be an AuditRecord or mapping")
    outcome = str(machine.get("outcome", "") or "")
    if not outcome:
        if machine.get("execution_executed"):
            outcome = "executed"
        elif machine.get("execution_attempted"):
            outcome = "rejected"
        elif machine.get("revalidation_performed") and not machine.get(
                "revalidation_valid"):
            outcome = "revalidation_failed"
        elif not machine.get("approval_approved", True):
            outcome = "not_approved"
        else:
            outcome = "not_executed"
    key_map = {"executed": "audit.execution_completed",
               "rejected": "audit.execution_rejected",
               "revalidation_failed": "audit.revalidation_failed",
               "not_approved": "audit.rejected_path",
               "not_executed": "audit.proposal_received"}
    summary = get_message(
        key_map.get(outcome, "audit.proposal_received"), code,
        record_id=str(machine.get("record_id", "")),
        proposal_hash=str(machine.get("proposal_hash", "")),
        reason=str(machine.get("revalidation_reason", "")
                   or machine.get("execution_reason", "")
                   or machine.get("stop_reason", "")),
        decision=str(machine.get("approval_approved", "")),
        outcome=outcome)
    if code == "fa":
        summary = bidi.isolate_machine_segments(summary)
    view = dict(machine)
    view["display_language"] = code
    view["display_summary"] = summary
    view["display_direction"] = bidi.direction_of(code)
    return view


__all__ = ("MACHINE_AUDIT_FIELDS", "MACHINE_STAGING_FIELDS",
           "TranslationRecord", "TranslationStatus",
           "build_translation_record", "is_translation_current",
           "present_audit_record", "present_staging_note",
           "translate_journal_entry")
