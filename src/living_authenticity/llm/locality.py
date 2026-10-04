"""Endpoint locality resolution for the network privacy gate.

``provider name != network locality``. A provider class named after a
local tool pointed at a remote host still transmits data off the
machine, so locality is always derived from the resolved endpoint
host. Missing or unparseable endpoints are treated as non-local
(fail closed).
"""
from urllib.parse import urlparse

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def resolve_endpoint_host(endpoint: str) -> str:
    """Return the lowercase host of ``endpoint``, or ``""`` if unknown."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        return ""
    text = endpoint.strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlparse(text)
        host = parsed.hostname
    except ValueError:
        return ""
    return (host or "").lower()


def is_local_endpoint(endpoint: str) -> bool:
    """True only when the endpoint demonstrably stays on this machine."""
    host = resolve_endpoint_host(endpoint)
    if not host:
        return False
    return host in _LOCAL_HOSTS or host.startswith("127.")
