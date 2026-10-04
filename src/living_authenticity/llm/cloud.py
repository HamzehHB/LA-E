"""Cloud provider implementation — one of several possible shapes.

This module implements the common ``LLMProvider`` interface for one
concrete cloud request/response shape as an example (a JSON POST to a
configurable endpoint with an ``Authorization: Bearer`` header and a
``choices``-style response envelope). It is deliberately *not* described
as "the" cloud provider: any cloud API with a different request format,
authentication header, or response parsing is added as one new small
module under ``llm/`` plus one registry entry, without modifying this
file and without touching the shared interface. Nothing here names a
vendor or a model.

Model and endpoint are configuration, never code: change them in
``Config/llm.local.yaml`` (or the tracked ``Config/llm.example.yaml``
defaults) to switch. Credentials come from the environment variable
named in configuration; they are never read from YAML, never logged,
and never recorded in audit records. Every cloud provider is subject to
the same endpoint-based privacy gate (Addendum B) regardless of shape:
locality is resolved from the configured endpoint host, never from the
provider name.
"""
import os

from .base import LLMProvider, LLMUnavailable
from .http import post_json


class CloudProvider(LLMProvider):
    """One cloud request/response shape behind the common interface."""

    def __init__(self, model: str, endpoint: str,
                 credential_env: str = "", timeout_seconds: int = 120,
                 max_tokens: int = 1024, temperature: float = 0.0) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if credential_env is not None and not isinstance(credential_env, str):
            raise ValueError("credential_env must be a string")
        self._model = model.strip()
        self._endpoint = endpoint.strip().rstrip("/")
        self._credential_env = (credential_env or "").strip()
        self._timeout = timeout_seconds
        self._max_tokens = max_tokens
        self._temperature = float(temperature)

    @property
    def name(self) -> str:
        return "cloud"

    @property
    def model(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def credential_env(self) -> str:
        """Environment variable name only — never the credential value."""
        return self._credential_env

    def is_credential_configured(self) -> bool:
        """True when the named environment variable holds a value."""
        if not self._credential_env:
            return False
        return bool(os.environ.get(self._credential_env, "").strip())

    def complete(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMUnavailable("prompt must be a non-empty string")
        if not self._credential_env:
            raise LLMUnavailable(
                "cloud provider requires llm.credential_env to be set")
        secret = os.environ.get(self._credential_env, "").strip()
        if not secret:
            raise LLMUnavailable(
                "cloud credential environment variable is not set")
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._temperature,
            "max_tokens": self._max_tokens,
        }
        data = post_json(
            self._endpoint + "/v1/chat/completions", payload, self._timeout,
            headers={"Authorization": "Bearer " + secret},
        )
        if not isinstance(data, dict):
            raise LLMUnavailable("unexpected cloud response shape")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMUnavailable("cloud response contains no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMUnavailable("cloud response choice is malformed")
        message = first.get("message")
        if not isinstance(message, dict):
            raise LLMUnavailable("cloud response message is malformed")
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise LLMUnavailable("cloud returned an empty response")
        return text