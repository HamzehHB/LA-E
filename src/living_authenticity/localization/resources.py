"""Static localized-resource loading and contract validation."""
import json
import re
from pathlib import Path

from .config import SUPPORTED_LANGUAGES, normalize_language

RESOURCES_DIR = Path(__file__).resolve().parent / "resources"

SUPPORTED_DOMAINS = ("app", "staging", "audit")

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Machine-identifier placeholder names that are allowed to appear in
# localized templates. Any other brace placeholder is still checked for
# parity, but these document the machine-stable contract explicitly.
MACHINE_PLACEHOLDERS = frozenset({
    "record_id", "run_id", "query_unit_id", "proposal_hash", "action",
    "decision", "outcome", "reason", "title", "count",
    "target_language", "source_language",
})

_cache: dict = {}


def _resource_path(language: str) -> Path:
    return RESOURCES_DIR / (normalize_language(language) + ".json")


def load_resources(language: str) -> dict:
    """Load one language resource file (cached, deterministic)."""
    code = normalize_language(language)
    if code in _cache:
        return dict(_cache[code])
    path = _resource_path(code)
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ValueError("missing localization resource: %s" % path) from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            "malformed localization resource %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("localization resource must be a mapping: %s" % path)
    for key, value in data.items():
        if not isinstance(key, str) or not key:
            raise ValueError("invalid localization key in %s" % path)
        if not isinstance(value, str):
            raise ValueError("localization value must be a string: %r" % key)
    _cache[code] = dict(data)
    return dict(data)


def list_keys() -> tuple:
    """Return the sorted union of keys across supported languages."""
    keys: set = set()
    for code in SUPPORTED_LANGUAGES:
        keys.update(load_resources(code).keys())
    return tuple(sorted(keys))


def _placeholders(template: str) -> frozenset:
    return frozenset(_PLACEHOLDER_RE.findall(template or ""))


def get_message(key: str, language: str = "en", **kwargs) -> str:
    """Render one static message; explicit fallback, never silent mixing.

    The requested language is used when it provides a non-empty template.
    Otherwise the English template is used and the result carries an
    explicit ``[missing:{lang}:{key}]`` marker so gaps stay visible.
    Unknown keys raise ``KeyError``. Missing formatting arguments raise
    ``KeyError`` deterministically instead of producing partial output.
    """
    code = normalize_language(language)
    catalog = load_resources(code)
    template = catalog.get(key, "")
    if isinstance(template, str) and template.strip():
        used = template
        missing = False
    else:
        used = load_resources("en").get(key, "")
        if not isinstance(used, str) or not used.strip():
            raise KeyError("unknown localization key: %r" % key)
        missing = True
    expected = _placeholders(used)
    provided = {k for k, v in kwargs.items()}
    if not expected.issubset(provided):
        absent = sorted(expected - provided)
        raise KeyError("missing placeholders %r for key %r" % (absent, key))
    safe = {name: (value if isinstance(value, str) else str(value))
            for name, value in kwargs.items() if name in expected}
    rendered = used.format(**safe)
    if missing:
        return "[missing:%s:%s] %s" % (code, key, rendered)
    return rendered


def validate_resources() -> dict:
    """Validate parity/determinism across en/fa; return a report dict."""
    catalogs = {code: load_resources(code) for code in SUPPORTED_LANGUAGES}
    base = set(catalogs["en"].keys())
    report: dict = {"ok": True, "missing_keys": {}, "extra_keys": {},
                    "empty_values": {}, "placeholder_mismatches": {},
                    "domain_gaps": {}, "errors": []}
    for code in SUPPORTED_LANGUAGES:
        keys = set(catalogs[code].keys())
        missing = sorted(base - keys)
        extra = sorted(keys - base) if code != "en" else []
        if missing:
            report["missing_keys"][code] = missing
        if extra:
            report["extra_keys"][code] = extra
        empties = sorted(k for k, v in catalogs[code].items()
                         if not isinstance(v, str) or not v.strip())
        if empties:
            report["empty_values"][code] = empties
    for key in sorted(base):
        expected = _placeholders(catalogs["en"].get(key, ""))
        for code in SUPPORTED_LANGUAGES:
            if code == "en":
                continue
            actual = _placeholders(catalogs[code].get(key, ""))
            if actual != expected:
                report["placeholder_mismatches"][key] = {
                    "en": sorted(expected), code: sorted(actual)}
    for domain in SUPPORTED_DOMAINS:
        present = [k for k in base if k == domain or k.startswith(domain + ".")]
        if not present:
            report["domain_gaps"][domain] = []
    report["ok"] = not (report["missing_keys"] or report["extra_keys"]
                        or report["empty_values"]
                        or report["placeholder_mismatches"]
                        or report["domain_gaps"] or report["errors"])
    return report


__all__ = ("MACHINE_PLACEHOLDERS", "RESOURCES_DIR", "SUPPORTED_DOMAINS",
           "get_message", "list_keys", "load_resources",
           "validate_resources")
