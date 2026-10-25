"""Continuous multi-stage narrative audit generation.

Generates human-readable, multi-stage explanatory narratives across the
lifecycle:
1. Input & Ingestion (source origin, unit extraction, normalization)
2. Retrieval (5 capabilities: query prep, candidate retrieval, relevance, candidate IDs, evidence)
3. Governance & Decision Boundary (revalidation checks, explicit human review)
4. Execution & Staging (controlled executor, atomic publishing, destination, digests)
5. Durability & Recovery (audit records, emergency journaling, quarantine if unreconciled)

Supports localized presentation (English 'en' and Persian 'fa') without
modifying machine records, and integrates with TranslationRecord for traceable
bilingual audit records.
"""

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from typing import Any

from src.living_authenticity.localization.config import normalize_language
from src.living_authenticity.localization.present import (
    TranslationRecord,
    TranslationStatus,
    build_translation_record,
    is_translation_current,
)


@dataclass(frozen=True)
class NarrativeContext:
    """Pre-computed context for lifecycle narrative generation prior to AuditRecord creation."""

    record_id: str = ""
    run_id: str = ""
    query_unit_id: str = ""
    query_source: str = ""
    query_position: int = 0
    proposal_action: str = ""
    proposal_hash: str = ""
    terminal_outcome: str = ""
    approval_approved: bool = False
    revalidation_valid: bool = False
    revalidation_reason: str = ""
    execution_executed: bool = False
    execution_reason: str = ""
    artifact_path: str = ""
    artifact_reference: str = ""
    artifact_sha256: str = ""
    retrieval_strategy: str = ""
    retrieval_candidates: tuple = ()
    retrieval_note: str = ""
    timestamp: str = ""
    narrative_language: str = "en"


def _safe_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def generate_lifecycle_narrative(
    record: Any,
    language: str = "en",
) -> str:
    """Generate a multi-stage explanatory narrative for an AuditRecord in English or Persian."""
    lang = normalize_language(language)

    rec_id = _safe_str(getattr(record, "record_id", ""))
    run_id = _safe_str(getattr(record, "run_id", ""))
    unit_id = _safe_str(getattr(record, "query_unit_id", ""))
    source = _safe_str(getattr(record, "query_source", ""))
    pos = getattr(record, "query_position", 0)
    action = _safe_str(getattr(record, "proposal_action", ""))
    prop_hash = _safe_str(getattr(record, "proposal_hash", ""))
    term_outcome = _safe_str(getattr(record, "terminal_outcome", ""))
    approved = bool(getattr(record, "approval_approved", False))
    reval_valid = bool(getattr(record, "revalidation_valid", False))
    reval_reason = _safe_str(getattr(record, "revalidation_reason", ""))
    exec_executed = bool(getattr(record, "execution_executed", False))
    exec_reason = _safe_str(getattr(record, "execution_reason", ""))
    art_path = _safe_str(getattr(record, "artifact_path", "") or getattr(record, "artifact_reference", ""))
    art_digest = _safe_str(getattr(record, "artifact_sha256", ""))
    ret_strategy = _safe_str(getattr(record, "retrieval_strategy", ""))
    ret_candidates = getattr(record, "retrieval_candidates", ()) or ()
    ret_note = _safe_str(getattr(record, "retrieval_note", ""))
    timestamp = _safe_str(getattr(record, "timestamp", ""))

    cand_count = len(ret_candidates)
    reval_desc = "valid" if reval_valid else (f"failed ({reval_reason})" if reval_reason else "invalid")
    decision_desc = "approved by human reviewer" if approved else "not approved or held"
    target_dest = _safe_str(getattr(record, "destination", ""))
    quarantine_dest = target_dest if (".unreconciled" in target_dest) else (art_path if ".unreconciled" in art_path else "")
    if term_outcome == "audit_persistence_failed":
        if "partial quarantine" in exec_reason:
            exec_desc = f"executed but audit persistence failed; partial quarantine (source cleanup failed; source remains in staging at `{art_path}`, destination link created at `{quarantine_dest or art_path}`)"
        elif quarantine_dest:
            exec_desc = f"executed but audit persistence failed; artifact quarantined to `{quarantine_dest}`"
        else:
            exec_desc = f"executed but audit persistence failed; quarantine failed (artifact remains in staging at `{art_path}`)" if art_path else "executed but audit persistence failed; quarantine failed"
    else:
        exec_desc = "successfully published to staging" if exec_executed else (f"halted/rejected ({exec_reason})" if exec_reason else "not executed")

    if lang == "fa":
        fa_reval = "معتبر و تاییدشده" if reval_valid else (f"نامعتبر ({reval_reason})" if reval_reason else "نامعتبر")
        fa_decision = "تاییدشده توسط بازبین انسانی" if approved else "تاییدنشده یا معلق"
        if term_outcome == "audit_persistence_failed":
            if "partial quarantine" in exec_reason:
                fa_exec = f"اجرا شد اما ثبت پایدار بررسی ناموفق بود؛ قرنطینه ناقص (پاکسازی مبدا ناموفق؛ مبدا در `{art_path}` باقی ماند، پیوند مقصد: `{quarantine_dest or art_path}`)"
            elif quarantine_dest:
                fa_exec = f"اجرا شد اما ثبت پایدار بررسی ناموفق بود؛ پرونده در `{quarantine_dest}` قرنطینه شد"
            else:
                fa_exec = f"اجرا شد اما ثبت پایدار بررسی و قرنطینه ناموفق بود (پرونده در `{art_path}` باقی ماند)" if art_path else "اجرا شد اما ثبت پایدار بررسی و قرنطینه ناموفق بود"
        else:
            fa_exec = "با موفقیت در مرحله پیش‌نویس موقت منتشر شد" if exec_executed else (f"متوقف/رد شده ({exec_reason})" if exec_reason else "اجرا نشده")

        lines = [
            f"### گزارش حسابرسی پیوسته و شفاف (شناسه: {rec_id or 'نامشخص'})",
            f"- **شناسه اجرا**: `{run_id}` | **زمان ثبت**: `{timestamp}`",
            "",
            "#### ۱. مرحله ورودی و استخراج دانش",
            f"- منبع ورودی: `{source or 'متن مستقیم'}` (موقعیت: {pos})",
            f"- شناسه واحد دانش: `{unit_id}`",
            "",
            "#### ۲. مرحله بازیابی و شواهد زمینه‌ای (قابلیت‌های ۵گانه)",
            f"- روش بازیابی: `{ret_strategy or 'token_overlap'}`",
            f"- تعداد نامزدهای زمینه‌ای بازیابی‌شده: {cand_count} مورد",
        ]
        if ret_candidates:
            lines.append("- شواهد نامزدهای بازیابی‌شده:")
            for cand in ret_candidates:
                c_id = _safe_str(getattr(cand, "unit_id", "") or (cand.get("unit_id") if isinstance(cand, dict) else ""))
                c_src = _safe_str(getattr(cand, "source", "") or (cand.get("source") if isinstance(cand, dict) else ""))
                c_dist = getattr(cand, "vector_distance", None) if hasattr(cand, "vector_distance") else (cand.get("vector_distance") if isinstance(cand, dict) else None)
                if c_dist is not None:
                    lines.append(f"  * واحد `{c_id}` از `{c_src}` با فاصله برداری {float(c_dist):.4f}")
                else:
                    c_score = getattr(cand, "overlap_score", 0) if hasattr(cand, "overlap_score") else (cand.get("overlap_score", 0) if isinstance(cand, dict) else 0)
                    lines.append(f"  * واحد `{c_id}` از `{c_src}` با امتیاز همپوشانی {c_score}")
        if ret_note:
            lines.append(f"- یادداشت بازیابی: {ret_note}")
        lines.extend([
            "",
            "#### ۳. مرز نظارت، اعتبارسنجی و تصمیم‌گیری انسانی",
            f"- اقدام پیشنهادی: `{action}` (شناسه درهم‌ساز: `{prop_hash}`)",
            f"- وضعیت اعتبارسنجی مجدد: {fa_reval}",
            f"- تصمیم بازبین انسانی: {fa_decision}",
            "",
            "#### ۴. مرحله اجرای کنترل‌شده و پیش‌نویس موقت",
            f"- نتیجه نهایی اجرا: {fa_exec}",
        ])
        if art_path:
            lines.append(f"- مسیر پرونده قرنطینه‌شده: `{art_path}`" if term_outcome == "audit_persistence_failed" else f"- مسیر فایل پیش‌نویس: `{art_path}`")
        if art_digest:
            lines.append(f"- چکیده امنیتی (SHA-256): `{art_digest}`")
        lines.extend([
            "",
            f"#### ۵. سرانجام نهایی: `{term_outcome}`",
        ])
        return "\n".join(lines)

    # Default: English
    lines = [
        f"### Continuous Lifecycle Narrative Audit (Record: {rec_id or 'unbound'})",
        f"- **Run ID**: `{run_id}` | **Timestamp**: `{timestamp}`",
        "",
        "#### 1. Input & Knowledge Ingestion",
        f"- Source Origin: `{source or 'direct_text'}` (Position: {pos})",
        f"- Knowledge Unit ID: `{unit_id}`",
        "",
        "#### 2. Retrieval & Contextual Evidence (5 Capabilities)",
        f"- Retrieval Strategy: `{ret_strategy or 'token_overlap'}`",
        f"- Contextual Candidates Retrieved: {cand_count}",
    ]
    if ret_candidates:
        lines.append("- Candidate Evidence:")
        for cand in ret_candidates:
            c_id = _safe_str(getattr(cand, "unit_id", "") or (cand.get("unit_id") if isinstance(cand, dict) else ""))
            c_src = _safe_str(getattr(cand, "source", "") or (cand.get("source") if isinstance(cand, dict) else ""))
            c_dist = getattr(cand, "vector_distance", None) if hasattr(cand, "vector_distance") else (cand.get("vector_distance") if isinstance(cand, dict) else None)
            if c_dist is not None:
                lines.append(f"  * Unit `{c_id}` from `{c_src}` with vector distance {float(c_dist):.4f}")
            else:
                c_score = getattr(cand, "overlap_score", 0) if hasattr(cand, "overlap_score") else (cand.get("overlap_score", 0) if isinstance(cand, dict) else 0)
                lines.append(f"  * Unit `{c_id}` from `{c_src}` with overlap score {c_score}")
    if ret_note:
        lines.append(f"- Retrieval Limits / Note: {ret_note}")
    lines.extend([
        "",
        "#### 3. Governance, Revalidation & Human Decision Boundary",
        f"- Proposed Action: `{action}` (Hash: `{prop_hash}`)",
        f"- Revalidation Status: {reval_desc}",
        f"- Human Review Decision: {decision_desc}",
        "",
        "#### 4. Controlled Execution & Staging",
        f"- Execution Status: {exec_desc}",
    ])
    if art_path:
        lines.append(f"- Quarantined Staging Artifact Path: `{art_path}`" if term_outcome == "audit_persistence_failed" else f"- Staging Artifact Path: `{art_path}`")
    if art_digest:
        lines.append(f"- Artifact Digest (SHA-256): `{art_digest}`")
    lines.extend([
        "",
        f"#### 5. Terminal Outcome: `{term_outcome}`",
    ])
    return "\n".join(lines)


def build_narrative_translation_record(
    record: Any,
    target_language: str = "fa",
    translator: Callable[[str, str, str], str] | None = None,
) -> TranslationRecord:
    """Build a TranslationRecord pairing the source narrative with the target language."""
    source_lang = normalize_language(getattr(record, "narrative_language", "en") or "en")
    target_lang = normalize_language(target_language)
    rec_id = _safe_str(getattr(record, "record_id", ""))

    source_text = _safe_str(getattr(record, "narrative", ""))
    if not source_text:
        source_text = generate_lifecycle_narrative(record, language=source_lang)

    if target_lang == source_lang:
        return build_translation_record(
            source_kind="audit_narrative",
            source_id=rec_id,
            source_text=source_text,
            source_language=source_lang,
            target_language=target_lang,
            translated_text=source_text,
            translator="identity",
            status=TranslationStatus.CURRENT,
        )

    # Helper to preserve custom source notes in generated text
    def _attach_custom_notes(base_translated: str) -> str:
        default_source = generate_lifecycle_narrative(record, language=source_lang)
        if source_text.strip() != default_source.strip():
            default_lines = set(default_source.strip().splitlines())
            custom_lines = [line for line in source_text.strip().splitlines() if line not in default_lines]
            if custom_lines:
                custom_block = "\n".join(custom_lines)
                if target_lang == "fa":
                    return base_translated + f"\n\n#### یادداشت‌های تکمیلی منبع (حفظ‌شده بدون تغییر):\n{custom_block}"
                else:
                    return base_translated + f"\n\n#### Custom Source Notes (Preserved):\n{custom_block}"
        return base_translated

    # Target is different from source
    if callable(translator):
        try:
            custom_rendered = translator(source_text, source_lang, target_lang)
            if isinstance(custom_rendered, str) and custom_rendered.strip():
                translated_text = custom_rendered
                translator_name = "custom-translator"
                status = TranslationStatus.CURRENT
            else:
                translated_text = _attach_custom_notes(generate_lifecycle_narrative(record, language=target_lang))
                translator_name = "template-fallback"
                status = TranslationStatus.CURRENT
        except Exception:
            translated_text = _attach_custom_notes(generate_lifecycle_narrative(record, language=target_lang))
            translator_name = "template-fallback"
            status = TranslationStatus.CURRENT
    else:
        translator_name = "template-localization-v1"
        translated_text = _attach_custom_notes(generate_lifecycle_narrative(record, language=target_lang))
        status = TranslationStatus.CURRENT

    return build_translation_record(
        source_kind="audit_narrative",
        source_id=rec_id,
        source_text=source_text,
        source_language=source_lang,
        target_language=target_lang,
        translated_text=translated_text,
        translator=translator_name,
        status=status,
    )


def is_narrative_translation_stale(record: Any, translation: TranslationRecord) -> bool:
    """Return True if the audit narrative has diverged from the translation record."""
    source_lang = normalize_language(getattr(record, "narrative_language", "en") or "en")
    current_source_text = _safe_str(getattr(record, "narrative", ""))
    if not current_source_text:
        current_source_text = generate_lifecycle_narrative(record, language=source_lang)
    return not is_translation_current(translation, current_source_text)
