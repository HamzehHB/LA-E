"""Independent language-domain configuration (app / staging / audit)."""
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_LANGUAGES = ("en", "fa")

APP_LANGUAGE_KEY = "language.app"
STAGING_LANGUAGE_KEY = "language.staging"
AUDIT_LANGUAGE_KEY = "language.audit"

DEFAULT_APP_LANGUAGE = "en"
DEFAULT_STAGING_LANGUAGE = "en"
DEFAULT_AUDIT_LANGUAGE = "en"

_LOCALIZATION_EXAMPLE_FILE = "localization.example.yaml"
_LOCALIZATION_LOCAL_FILE = "localization.local.yaml"


class LanguageConfigError(ValueError):
    """Raised when a language configuration value is invalid."""


def normalize_language(value) -> str:
    """Return canonical ``en``/``fa`` code or raise ``LanguageConfigError``."""
    text = value.strip().lower() if isinstance(value, str) else ""
    # Accept a few explicit aliases; anything else is rejected deliberately.
    aliases = {"english": "en", "persian": "fa", "farsi": "fa", "fa-ir": "fa"}
    canonical = aliases.get(text, text)
    if canonical not in SUPPORTED_LANGUAGES:
        raise LanguageConfigError(
            "unsupported language: %r (supported: en, fa)" % (value,))
    return canonical


@dataclass(frozen=True)
class LanguageConfig:
    """Three independent language domains; never collapsed to one value."""

    app_language: str = DEFAULT_APP_LANGUAGE
    staging_language: str = DEFAULT_STAGING_LANGUAGE
    audit_language: str = DEFAULT_AUDIT_LANGUAGE

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_language",
                           normalize_language(self.app_language))
        object.__setattr__(self, "staging_language",
                           normalize_language(self.staging_language))
        object.__setattr__(self, "audit_language",
                           normalize_language(self.audit_language))


def _lookup(data: dict, dotted: str):
    value = data
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def load_language_config(config_dir=None) -> LanguageConfig:
    """Load the three independent language settings with explicit defaults."""
    import yaml

    from Config.settings import CONFIG_DIR
    base = Path(config_dir) if config_dir is not None else Path(CONFIG_DIR)
    example = base / _LOCALIZATION_EXAMPLE_FILE
    if not example.is_file():
        return LanguageConfig()
    try:
        with example.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except Exception as exc:
        raise LanguageConfigError(
            "could not read %s: %s" % (example, exc)) from exc
    if not isinstance(data, dict):
        raise LanguageConfigError("localization config must be a mapping")
    local = base / _LOCALIZATION_LOCAL_FILE
    if local.is_file():
        try:
            with local.open("r", encoding="utf-8") as handle:
                override = yaml.safe_load(handle) or {}
        except Exception as exc:
            raise LanguageConfigError(
                "could not read %s: %s" % (local, exc)) from exc
        if not isinstance(override, dict):
            raise LanguageConfigError("localization override must be a mapping")
        merged = dict(data)
        raw_base = data.get("language")
        base_lang = raw_base if isinstance(raw_base, dict) else {}
        raw_over = override.get("language")
        over_lang = raw_over if isinstance(raw_over, dict) else {}
        combined = dict(base_lang)
        combined.update(over_lang)
        merged["language"] = combined
        for key, value in override.items():
            if key != "language":
                merged[key] = value
        data = merged
    values = {}
    defaults = {"app": DEFAULT_APP_LANGUAGE, "staging": DEFAULT_STAGING_LANGUAGE,
                "audit": DEFAULT_AUDIT_LANGUAGE}
    for domain, default in defaults.items():
        raw = _lookup(data, "language." + domain)
        if raw is None:
            values[domain] = default
            continue
        values[domain] = normalize_language(raw)
    return LanguageConfig(app_language=values["app"],
                          staging_language=values["staging"],
                          audit_language=values["audit"])


__all__ = ("APP_LANGUAGE_KEY", "AUDIT_LANGUAGE_KEY", "STAGING_LANGUAGE_KEY",
           "SUPPORTED_LANGUAGES", "DEFAULT_APP_LANGUAGE",
           "DEFAULT_AUDIT_LANGUAGE", "DEFAULT_STAGING_LANGUAGE",
           "LanguageConfig", "LanguageConfigError", "load_language_config",
           "normalize_language")
