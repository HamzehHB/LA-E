"""Deterministic bidi strategy: RTL prose, LTR machine values."""
import re

_RLI_CODE = 0x2066
_LRI_CODE = 0x2068
_PDI_CODE = 0x206C
RLI = chr(_RLI_CODE)
LRI = chr(_LRI_CODE)
PDI = chr(_PDI_CODE)

_PERSIAN_RE = re.compile(
    "[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF"
    "\uFB50-\uFDFF\uFE70-\uFEFF]")


def contains_persian(text: str) -> bool:
    return bool(_PERSIAN_RE.search(text or ""))


def direction_of(language_or_text: str) -> str:
    value = (language_or_text or "").strip().lower()
    if value in ("fa", "farsi", "persian", "fa-ir"):
        return "rtl"
    if contains_persian(language_or_text or ""):
        return "rtl"
    return "ltr"


def wrap_ltr(value: str) -> str:
    text = "" if value is None else str(value)
    if text.startswith(LRI) and text.endswith(PDI):
        return text
    return LRI + text + PDI


def wrap_rtl(value: str) -> str:
    text = "" if value is None else str(value)
    if text.startswith(RLI) and text.endswith(PDI):
        return text
    return RLI + text + PDI


_MACHINE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s]*)"
    r"|(?:/[^\s]*)"
    r"|(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"|(?:[0-9a-fA-F]{8,})"
    r"|(?:[\w.\-]+\.(?:md|txt|yaml|yml|json|jsonl))"
    r"|(?:rec-[0-9a-zA-Z]+|run-[0-9a-zA-Z\-]+)"
    r"|(?:[A-Z][A-Z0-9_]{2,})"
    r"|(?:https?://[^\s]+)"
    r"|(?:\d[\d:/\-.,\s]*\d|\d)")


def isolate_machine_segments(text: str) -> str:
    source = "" if text is None else str(text)

    def _replace(match) -> str:
        segment = match.group(0)
        if segment.startswith(LRI):
            return segment
        return wrap_ltr(segment)

    return _MACHINE_RE.sub(_replace, source)


def format_mixed(language: str, template: str, **kwargs) -> str:
    from .config import normalize_language
    code = normalize_language(language)
    rendered = template.format(**{
        k: (v if isinstance(v, str) else str(v))
        for k, v in kwargs.items()})
    if code == "fa":
        return isolate_machine_segments(rendered)
    return rendered


__all__ = ("LRI", "PDI", "RLI", "contains_persian", "direction_of",
           "format_mixed", "isolate_machine_segments", "wrap_ltr",
           "wrap_rtl")
