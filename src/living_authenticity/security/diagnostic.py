"""Centralized diagnostic sanitization and safe exception formatting.

Ensures that audit records, event telemetry, and user-facing error messages
never leak credentials, tokens, sensitive URLs, private input snippets,
untrusted control characters, or absolute host filesystem paths.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.living_authenticity.security.sensitive_data import (
    MACHINE_SPECIFIC_PATH_PREFIXES,
    SECRET_PATTERNS,
)

MAX_DIAGNOSTIC_LENGTH = 300
_UNIX_ROOTS = r"home|Users|root|mnt|media|Volumes|private|tmp|var|etc|opt|usr|srv|data|Library|Applications|System|proc|sys"


def sanitize_diagnostic_text(text: object, max_length: int = MAX_DIAGNOSTIC_LENGTH) -> str:
    """Sanitize raw diagnostic text fail-closed before persistence or event emission.

    - Removes ANSI escapes and untrusted control characters.
    - Flattens multi-line exception messages to a single line.
    - Redacts credentials, tokens, passwords, and authorization headers.
    - Redacts userinfo and credential query parameters from URLs.
    - Redacts verbatim user text snippets from parser exceptions.
    - Redacts absolute host filesystem paths while preserving basename where applicable.
    - Replaces known secret patterns from sensitive_data.SECRET_PATTERNS.
    - Enforces a deterministic upper bound on message length.
    """
    if not isinstance(text, str):
        text = str(text) if text is not None else ""
    if not text or max_length <= 0:
        return ""

    # 1. Strip ANSI escape sequences
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)

    # 2. Redact multi-line parser snippets before line flattening
    # YAML parser: lines following `in "...", line X, column Y:` with indented text
    text = re.sub(
        r'(in\s+["\'][^"\']+["\'],\s+line\s+\d+(?:,\s+column\s+\d+)?)\s*[\r\n]+[ \t]*[^\r\n]+',
        r"\1: <snippet redacted>",
        text,
    )

    # 3. Replace CR, LF, tabs, and non-printable control characters with a single space
    text = re.sub(r"[\r\n\t\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", text)

    # 4. Redact remaining inline parser snippets
    text = re.sub(r'(in\s+["\'][^"\']+["\'],\s+line\s+\d+,\s+column\s+\d+:)\s*.*$', r"\1 <snippet redacted>", text)
    text = re.sub(r'(line\s+\d+\s+column\s+\d+\s+\(char\s+\d+\)):\s*.*$', r"\1: <snippet redacted>", text)

    # 5. Redact authorization headers and Bearer tokens
    text = re.sub(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[^\s,;]+", r"\1<redacted>", text)
    text = re.sub(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1<redacted>", text)

    # 6. Redact key-value pairs (api_key=..., token: ..., password=..., secret=...), quoted or unquoted
    text = re.sub(
        r"""(?i)(["']?)((?:api[_-]?key|secret|token|password|passwd|auth(?:orization)?|credential)[a-z0-9_-]*)\1(\s*[:=]\s*)(?:(["'])[^"']*\4|[^\s,;'\"}&]+)""",
        r"\1\2\1\3<redacted>",
        text,
    )

    # 7. Redact known secret prefixes / patterns from sensitive_data.SECRET_PATTERNS
    for secret in SECRET_PATTERNS:
        if secret and secret in text:
            text = re.sub(re.escape(secret), "<redacted-secret>", text)

    # 8. Redact credentials in URLs:
    def _sanitize_uri_userinfo(match):
        scheme = match.group("scheme")
        userinfo = match.group("userinfo")
        if ":" in userinfo:
            user, _ = userinfo.split(":", 1)
            if user:
                return f"{scheme}{user}:<redacted>@"
            return f"{scheme}:<redacted>@"
        return f"{scheme}<redacted>@"

    # Userinfo across any scheme: scheme://userinfo@host
    text = re.sub(
        r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>[^/@?\s#]+)@",
        _sanitize_uri_userinfo,
        text,
    )
    # Query parameters: ?api_key=... or &token=...
    text = re.sub(
        r"(?i)([?&](?:api[_-]?key|client[_-]?secret|access[_-]?key|auth[_-]?token|key|token|access_token|secret|password|passwd|sig|signature)=)[^&\s'\"]+",
        r"\1<redacted>",
        text,
    )

    # 9. Redact machine-specific path prefixes configured locally
    for prefix in MACHINE_SPECIFIC_PATH_PREFIXES:
        if prefix and prefix in text:
            text = re.sub(re.escape(prefix), "<path>", text)

    # 10. Redact filesystem paths & usernames
    def _sanitize_quoted_path(match):
        raw_path = match.group("path")
        quote = match.group("quote")
        fn_match = re.search(r"[\/\\]([a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9_\-]+)$", raw_path)
        if fn_match:
            return f"{quote}<path>/{fn_match.group(1)}{quote}"
        return f"{quote}<path>{quote}"

    def _sanitize_unquoted_path(match):
        raw_path = match.group(0)
        fn_match = re.search(r"[\/\\]([a-zA-Z0-9_\-\.]+\.[a-zA-Z0-9_\-]+)$", raw_path)
        if fn_match:
            return f"<path>/{fn_match.group(1)}"
        return "<path>"

    # Quoted absolute paths (handles drive letters, UNC shares, and Unix root paths)
    text = re.sub(
        rf"(?P<quote>['\"])(?P<path>(?:[a-zA-Z]:[\\/]|\\\\|/(?:{_UNIX_ROOTS})[\\/])[^'\"]+)(?P=quote)",
        _sanitize_quoted_path,
        text,
    )
    # Unquoted Windows paths (drive letter + slash)
    text = re.sub(r"(?i)\b[a-z]:[\\/]+[^\s,:;'\"<>|]+", _sanitize_unquoted_path, text)
    # Unquoted UNC paths (\\\\server\share\...)
    text = re.sub(r"\\\\[^\s,:;'\"<>|]+", _sanitize_unquoted_path, text)
    # Unquoted Unix paths (negative lookbehind ensures it is not part of a URL)
    text = re.sub(
        rf"(?<![a-zA-Z0-9_.-])/(?:{_UNIX_ROOTS})/[^\s,:;'\"<>|]+",
        _sanitize_unquoted_path,
        text,
    )

    # Clean up multiple spaces
    text = re.sub(r"\s+", " ", text).strip()

    # 11. Bound message length
    suffix = "... [truncated]"
    if len(text) > max_length:
        if max_length <= len(suffix):
            text = text[:max_length]
        else:
            text = text[:max_length - len(suffix)] + suffix

    return text


def sanitize_path(path: object) -> str:
    """Return a path string with private directory and user prefixes removed."""
    if path is None:
        return ""
    if isinstance(path, Path):
        path_str = str(path)
    else:
        path_str = str(path)
    return sanitize_diagnostic_text(path_str)


def safe_exception_diagnostic(
    exc: object,
    stage: str = "",
    category: str = "",
    max_length: int = MAX_DIAGNOSTIC_LENGTH,
) -> tuple[str, tuple[str, ...]]:
    """Produce a safe (stop_reason, errors) pair from an exception or error string.

    Returns:
    - stop_reason: A concise single-line sanitized string suitable for UI and stop_reason.
    - errors: A 1-tuple of sanitized string(s) with the exception type preserved.
    """
    if isinstance(exc, BaseException):
        exc_type = type(exc).__name__
        raw_msg = str(exc) or exc_type
    elif isinstance(exc, str):
        if ": " in exc:
            parts = exc.split(": ", 1)
            if parts[0].isidentifier():
                exc_type = parts[0]
                raw_msg = parts[1]
            else:
                exc_type = "Error"
                raw_msg = exc
        else:
            exc_type = "Error"
            raw_msg = exc
    else:
        exc_type = "Error"
        raw_msg = str(exc) if exc is not None else ""

    exc_type = sanitize_diagnostic_text(exc_type, max_length=64) or "Error"
    sanitized_msg = sanitize_diagnostic_text(raw_msg, max_length=max_length)
    if not sanitized_msg:
        sanitized_msg = exc_type

    stop_reason = sanitized_msg
    errors = (f"{exc_type}: {sanitized_msg}",)
    return stop_reason, errors
