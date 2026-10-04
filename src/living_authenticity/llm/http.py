"""Minimal standard-library JSON-over-HTTP helper for LLM providers.

This module is the only place in application code that opens a network
connection, and it is used exclusively by the configured LLM provider.
It performs one request and returns the parsed JSON body; it never
retries, never falls back, and never reads or writes files. Failures
raise :class:`LLMUnavailable` so callers fail safe.
"""
import json
import urllib.error
import urllib.request

from .base import LLMUnavailable


def post_json(url: str, payload: dict, timeout_seconds: int,
              headers: dict | None = None) -> dict:
    """POST ``payload`` as JSON to ``url`` and return the parsed body.

    ``headers`` may carry a credential header for a configured cloud
    provider. The credential is never logged, stored, or returned.
    """
    if not isinstance(url, str) or not url.strip():
        raise LLMUnavailable("endpoint url must be a non-empty string")
    if not isinstance(payload, dict):
        raise LLMUnavailable("payload must be a mapping")
    body = json.dumps(payload).encode("utf-8")
    outbound = {"Content-Type": "application/json"}
    if headers:
        outbound.update(headers)
    request = urllib.request.Request(
        url,
        data=body,
        headers=outbound,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LLMUnavailable(
            "LLM endpoint unreachable: " + type(exc).__name__
        ) from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LLMUnavailable("LLM endpoint returned invalid JSON") from exc