"""Tests for continuous multi-stage narrative audit generation."""

from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord
from src.living_authenticity.knowledge.governance.audit.narrative import (
    generate_lifecycle_narrative,
    build_narrative_translation_record,
)
from src.living_authenticity.knowledge.analysis.retrieval.candidate import RetrievedCandidate
from src.living_authenticity.localization.present import TranslationRecord, is_translation_current


def _sample_record(**kwargs) -> AuditRecord:
    rec = AuditRecord(
        record_id="rec-test-1234",
        run_id="run-test-5678",
        query_unit_id="ku-narrative-test",
        query_source="source_file.md",
        query_position=2,
        proposal_action="CREATE",
        proposal_hash="hash-abc-123",
        terminal_outcome="executed",
        approval_approved=True,
        revalidation_performed=True,
        revalidation_valid=True,
        execution_attempted=True,
        execution_permitted=True,
        execution_executed=True,
        artifact_path="/staging/notes/note_1.md",
        artifact_sha256="sha256-artifact-digest",
        retrieval_strategy="token_overlap",
        retrieval_candidates=(
            RetrievedCandidate(
                unit_id="ku-cand-1",
                source="prior_note.md",
                position=0,
                text="prior note content with presence",
                matched_terms=("presence",),
                overlap_score=1,
                retriever="token_overlap",
                evidence_basis="Shares 1 content token(s) with query: [presence]",
                candidate_identity_key="prior_note.md#0@ku-cand-1",
            ),
        ),
        retrieval_note="Retrieved 1 of 5 examined units as contextual evidence",
        timestamp="2026-10-21T12:00:00Z",
    )
    if kwargs:
        import dataclasses
        return dataclasses.replace(rec, **kwargs)
    return rec


class TestAuditNarrative:
    def test_english_narrative_covers_all_five_stages(self):
        rec = _sample_record()
        narrative = generate_lifecycle_narrative(rec, language="en")

        assert "rec-test-1234" in narrative
        assert "run-test-5678" in narrative
        # Stage 1: Input & Ingestion
        assert "1. Input & Knowledge Ingestion" in narrative
        assert "source_file.md" in narrative
        assert "ku-narrative-test" in narrative
        # Stage 2: Retrieval
        assert "2. Retrieval & Contextual Evidence (5 Capabilities)" in narrative
        assert "token_overlap" in narrative
        assert "ku-cand-1" in narrative
        assert "prior_note.md" in narrative
        # Stage 3: Governance
        assert "3. Governance, Revalidation & Human Decision Boundary" in narrative
        assert "CREATE" in narrative
        assert "approved by human reviewer" in narrative
        # Stage 4: Execution
        assert "4. Controlled Execution & Staging" in narrative
        assert "/staging/notes/note_1.md" in narrative
        assert "sha256-artifact-digest" in narrative
        # Stage 5: Terminal
        assert "5. Terminal Outcome: `executed`" in narrative

    def test_persian_narrative_generates_rtl_localized_sections(self):
        rec = _sample_record()
        narrative_fa = generate_lifecycle_narrative(rec, language="fa")

        assert "گزارش حسابرسی پیوسته و شفاف" in narrative_fa
        assert "۱. مرحله ورودی و استخراج دانش" in narrative_fa
        assert "۲. مرحله بازیابی و شواهد زمینه‌ای (قابلیت‌های ۵گانه)" in narrative_fa
        assert "۳. مرز نظارت، اعتبارسنجی و تصمیم‌گیری انسانی" in narrative_fa
        assert "۴. مرحله اجرای کنترل‌شده و پیش‌نویس موقت" in narrative_fa
        assert "۵. سرانجام نهایی: `executed`" in narrative_fa
        # Machine identifiers remain preserved in English / code format
        assert "`rec-test-1234`" in narrative_fa or "rec-test-1234" in narrative_fa
        assert "`hash-abc-123`" in narrative_fa
        assert "`ku-narrative-test`" in narrative_fa

    def test_build_narrative_translation_record_creates_traceable_record(self):
        rec = _sample_record()
        trans_rec = build_narrative_translation_record(rec, target_language="fa")

        assert isinstance(trans_rec, TranslationRecord)
        assert trans_rec.source_kind == "audit_narrative"
        assert trans_rec.source_id == "rec-test-1234"
        assert trans_rec.source_language == "en"
        assert trans_rec.target_language == "fa"
        assert trans_rec.status == "current"
        assert trans_rec.source_hash != ""
        assert trans_rec.translated_hash != ""
        assert "گزارش حسابرسی" in trans_rec.translated_text
        assert is_translation_current(trans_rec, generate_lifecycle_narrative(rec, "en"))

    def test_bidirectional_translation_from_persian_to_english(self):
        rec = _sample_record(narrative_language="fa")
        trans_rec = build_narrative_translation_record(rec, target_language="en")

        assert trans_rec.source_language == "fa"
        assert trans_rec.target_language == "en"
        assert trans_rec.status == "current"
        assert "Continuous Lifecycle Narrative Audit" in trans_rec.translated_text
        from src.living_authenticity.knowledge.governance.audit.narrative import is_narrative_translation_stale
        assert not is_narrative_translation_stale(rec, trans_rec)

    def test_stale_translation_detection_when_narrative_diverges(self):
        rec = _sample_record(narrative="Original baseline narrative text")
        trans_rec = build_narrative_translation_record(rec, target_language="fa")

        from src.living_authenticity.knowledge.governance.audit.narrative import is_narrative_translation_stale
        assert not is_narrative_translation_stale(rec, trans_rec)

        # Mutate the record with new narrative
        import dataclasses
        modified_rec = dataclasses.replace(rec, narrative="Modified narrative text after manual review")
        assert is_narrative_translation_stale(modified_rec, trans_rec)

    def test_custom_translator_callback(self):
        rec = _sample_record()

        def custom_trans(text, src, tgt):
            return f"[CUSTOM TRANSLATION from {src} to {tgt}]: {text[:30]}"

        trans_rec = build_narrative_translation_record(rec, target_language="fa", translator=custom_trans)
        assert trans_rec.translator == "custom-translator"
        assert "[CUSTOM TRANSLATION from en to fa]" in trans_rec.translated_text

    def test_vector_distance_formatting_in_narrative(self):
        rec = _sample_record(
            retrieval_strategy="lancedb_vector",
            retrieval_candidates=(
                RetrievedCandidate(
                    unit_id="ku-vec-1",
                    source="vec_note.md",
                    position=1,
                    text="vector text",
                    overlap_score=0,
                    retriever="lancedb_vector",
                    evidence_basis="vector_distance=0.1234",
                    candidate_identity_key="candidate:ku-vec-1@vec_note.md#1",
                    vector_distance=0.1234,
                ),
            ),
        )
        en_narrative = generate_lifecycle_narrative(rec, language="en")
        assert "vector distance 0.1234" in en_narrative

        fa_narrative = generate_lifecycle_narrative(rec, language="fa")
        assert "فاصله برداری 0.1234" in fa_narrative

    def test_empty_record_handled_gracefully(self):
        empty_rec = AuditRecord()
        narrative = generate_lifecycle_narrative(empty_rec, language="en")
        assert "Continuous Lifecycle Narrative Audit" in narrative
        assert "1. Input & Knowledge Ingestion" in narrative
        assert "Contextual Candidates Retrieved: 0" in narrative

    def test_same_language_translation_returns_identity(self):
        rec = _sample_record(narrative_language="en")
        trans_rec = build_narrative_translation_record(rec, target_language="en")
        assert trans_rec.source_language == "en"
        assert trans_rec.target_language == "en"
        assert trans_rec.translator == "identity"
        assert trans_rec.translated_text == generate_lifecycle_narrative(rec, "en")
        assert trans_rec.source_hash == trans_rec.translated_hash

    def test_custom_translator_failure_falls_back_to_template(self):
        rec = _sample_record()

        def broken_translator(text, src, tgt):
            raise RuntimeError("translation service failure")

        trans_rec = build_narrative_translation_record(
            rec, target_language="fa", translator=broken_translator
        )
        assert trans_rec.translator == "template-fallback"
        assert trans_rec.status == "current"
        assert "گزارش حسابرسی" in trans_rec.translated_text
