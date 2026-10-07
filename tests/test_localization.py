"""Localization architecture tests: separation, preservation, RTL/LTR."""
import pytest

from src.living_authenticity.localization import bidi_core as bidi
from src.living_authenticity.localization import present
from src.living_authenticity.localization.config import (
    LanguageConfig, LanguageConfigError, load_language_config,
    normalize_language)
from src.living_authenticity.localization.present import (
    TranslationStatus, build_translation_record, is_translation_current,
    present_audit_record, present_staging_note, translate_journal_entry)
from src.living_authenticity.localization.resources import (
    get_message, load_resources, validate_resources)


def test_normalize_accepts_en_fa_and_rejects_others():
    assert normalize_language("en") == "en"
    assert normalize_language(" FA ") == "fa"
    assert normalize_language("farsi") == "fa"
    with pytest.raises(LanguageConfigError):
        normalize_language("de")
    with pytest.raises(LanguageConfigError):
        normalize_language("")


def test_three_domains_independent():
    cfg = LanguageConfig(app_language="fa", staging_language="en",
                         audit_language="fa")
    assert (cfg.app_language, cfg.staging_language,
            cfg.audit_language) == ("fa", "en", "fa")
    other = LanguageConfig(app_language="en", staging_language="fa",
                           audit_language="en")
    assert other.staging_language != cfg.staging_language
    assert other.app_language != cfg.app_language


def test_defaults_are_independent_english(tmp_path):
    (tmp_path / "localization.example.yaml").write_text(
        "language:\n  app: en\n  staging: en\n  audit: en\n",
        encoding="utf-8")
    cfg = load_language_config(tmp_path)
    assert cfg.app_language == "en"
    assert cfg.staging_language == "en"
    assert cfg.audit_language == "en"


def test_invalid_language_rejected(tmp_path):
    (tmp_path / "localization.example.yaml").write_text(
        "language:\n  app: de\n  staging: en\n  audit: en\n",
        encoding="utf-8")
    with pytest.raises((LanguageConfigError, Exception)):
        load_language_config(tmp_path)


def test_resource_parity_and_placeholders():
    report = validate_resources()
    assert report["ok"], report
    en = load_resources("en")
    fa = load_resources("fa")
    assert set(en.keys()) == set(fa.keys())
    for domain in ("app.", "staging.", "audit."):
        assert any(k.startswith(domain) for k in en)


def test_get_message_placeholders():
    text = get_message("app.process.needs_review", "en",
                       proposal_hash="abc123")
    assert "abc123" in text
    with pytest.raises(KeyError):
        get_message("no.such.key", "en")
    with pytest.raises(KeyError):
        get_message("app.process.needs_review", "en")


def test_persian_rtl_and_machine_isolation():
    sample = "گزارش rec-abc123 در C:/Data/note.md"
    isolated = bidi.isolate_machine_segments(sample)
    assert bidi.LRI in isolated and bidi.PDI in isolated
    assert bidi.direction_of("fa") == "rtl"
    assert bidi.direction_of("en") == "ltr"
    assert bidi.contains_persian("سلام")
    assert not bidi.contains_persian("hello")


def test_audit_presentation_keeps_machine_fields():
    record = {"record_id": "rec-1", "run_id": "run-1",
              "proposal_hash": "deadbeef1234", "query_unit_id": "q1",
              "proposal_action": "CREATE", "approval_approved": True,
              "revalidation_performed": True, "revalidation_valid": True,
              "execution_attempted": True, "execution_executed": True,
              "artifact_path": "C:/Staging/note.md",
              "timestamp": "2026-01-01T00:00:00"}
    en = present_audit_record(record, "en")
    fa = present_audit_record(record, "fa")
    for field in present.MACHINE_AUDIT_FIELDS:
        if field in record:
            assert en[field] == record[field]
            assert fa[field] == record[field]
    assert en["display_language"] == "en"
    assert fa["display_language"] == "fa"
    assert fa["display_direction"] == "rtl"


def test_staging_presentation_preserves_source():
    class Note:
        title = "Hello"
        body = "Source body"
        query_unit_id = "q-9"
    rec = present_staging_note(Note(), "fa")
    assert rec.source_kind == "staging"
    assert rec.source_id == "q-9"
    assert rec.target_language == "fa"
    assert rec.translated_text != ""


def test_translation_traceability_and_staleness():
    rec = build_translation_record("audit", "rec-1", "source text", "en",
                                   "fa", "matn", "test-t",
                                   TranslationStatus.CURRENT)
    assert is_translation_current(rec, "source text")
    assert not is_translation_current(rec, "changed text")
    stale = build_translation_record("audit", "rec-1", "source text", "en",
                                     "fa", "matn", "t",
                                     TranslationStatus.STALE)
    assert not is_translation_current(stale, "source text")


def test_translation_failure_preserves_source():
    def _boom(text, src, tgt):
        raise RuntimeError("no provider")
    rec = translate_journal_entry("staging", "p1", "original", "en", "fa",
                                  _boom, "boom")
    assert rec.status == TranslationStatus.UNAVAILABLE
    assert rec.translated_text == "original"


def test_translation_never_changes_machine_state():
    record = {"record_id": "rec-x", "proposal_hash": "hhhhhhhh",
              "approval_approved": False, "execution_executed": False,
              "revalidation_performed": False}
    view = present_audit_record(record, "fa")
    assert view["record_id"] == "rec-x"
    assert view["approval_approved"] is False
    assert view["execution_executed"] is False


def test_settings_load_languages(tmp_path):
    from Config.settings import load_languages
    (tmp_path / "localization.example.yaml").write_text(
        "language:\n  app: fa\n  staging: en\n  audit: fa\n",
        encoding="utf-8")
    data = load_languages(tmp_path)
    assert data == {"language": {"app": "fa", "staging": "en",
                                 "audit": "fa"}}


def test_new_review_keys_have_parity():
    import json as _json
    from pathlib import Path as _Path
    base = _Path("src/living_authenticity/localization/resources")
    en = _json.loads((base / "en.json").read_text(encoding="utf-8"))
    fa = _json.loads((base / "fa.json").read_text(encoding="utf-8"))
    assert len(en) == len(fa) == 54
    for key in ("app.process.review_header", "app.process.approval_prompt",
                "app.process.continue_prompt",
                "app.process.non_executable_notice",
                "app.process.label_proposal_hash",
                "app.process.run_summary"):
        assert key in en and key in fa
    report = validate_resources()
    assert report["ok"], report
