"""Localization layer: independent app / staging / audit language domains.

English (``en``) and Persian (``fa``) are the only supported languages.
The three domains are explicit, validated, and independent:

* ``app``     -- APP / UI / PROCESS presentation language.
* ``staging`` -- STAGING NOTE output language for newly generated content.
* ``audit``   -- AUDIT human-readable presentation language.

Localization is presentation-only: it never alters pipeline logic,
knowledge identity, provenance, evidence, machine identifiers, hashes,
paths, timestamps, enums, or governance boundaries. Static project text
comes from ``resources/en.json`` / ``resources/fa.json``; dynamic
knowledge content is never placed in those resources.
"""
from .bidi_core import (LRI, PDI, RLI, contains_persian, direction_of,
                   wrap_ltr, wrap_rtl)
from .config import (AUDIT_LANGUAGE_KEY, DEFAULT_AUDIT_LANGUAGE,
                     DEFAULT_STAGING_LANGUAGE, DEFAULT_APP_LANGUAGE,
                     STAGING_LANGUAGE_KEY, APP_LANGUAGE_KEY,
                     SUPPORTED_LANGUAGES, LanguageConfig,
                     load_language_config, normalize_language)
from .present import (TranslationRecord, TranslationStatus,
                      build_translation_record, is_translation_current,
                      present_audit_record, present_staging_note,
                      translate_journal_entry)
from .resources import (SUPPORTED_DOMAINS, get_message, list_keys,
                        load_resources, validate_resources)

__all__ = (
    "LRI",
    "PDI",
    "RLI",
    "APP_LANGUAGE_KEY",
    "AUDIT_LANGUAGE_KEY",
    "STAGING_LANGUAGE_KEY",
    "SUPPORTED_DOMAINS",
    "SUPPORTED_LANGUAGES",
    "DEFAULT_APP_LANGUAGE",
    "DEFAULT_AUDIT_LANGUAGE",
    "DEFAULT_STAGING_LANGUAGE",
    "LanguageConfig",
    "TranslationRecord",
    "TranslationStatus",
    "build_translation_record",
    "contains_persian",
    "direction_of",
    "get_message",
    "is_translation_current",
    "list_keys",
    "load_language_config",
    "load_resources",
    "normalize_language",
    "present_audit_record",
    "present_staging_note",
    "translate_journal_entry",
    "validate_resources",
    "wrap_ltr",
    "wrap_rtl",
)
