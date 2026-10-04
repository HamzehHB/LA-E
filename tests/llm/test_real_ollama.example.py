"""Synthetic counterpart to tests/local/test_real_ollama_local.py.

Proves the same LLM provider contract the real local file proves, using
a fake in-process HTTP server instead of a real Ollama installation, so
the tracked suite needs no local service and no production data.
"""
import pytest

from src.living_authenticity.llm import (
    LLMUnavailable,
    Synthesis,
    build_provider,
    build_synthesis,
)
from src.living_authenticity.llm.locality import is_local_endpoint
from tests.llm.fake_ollama_server import (
    FakeOllamaServer,
    install_no_proxy_opener,
    json_body,
)


def test_synthetic_local_ollama_end_to_end(monkeypatch):
    """Same contract as the real local test, with a synthetic endpoint."""
    install_no_proxy_opener(monkeypatch)
    body = json_body(title="Synthetic title",
                     body="Synthetic body text.",
                     suggested_type="Observation",
                     reason="synthetic reason",
                     uncertainty="synthetic uncertainty")
    with FakeOllamaServer(body) as server:
        assert is_local_endpoint(server.endpoint) is True
        config = {"llm": {"provider": "ollama", "model": "synthetic-model",
                          "endpoint": server.endpoint, "timeout_seconds": 10}}
        provider = build_provider(config)
        assert provider.requires_privacy_confirmation is False
        synthesis = build_synthesis(provider, config=config)
        result, prompt, digest = synthesis.synthesize()
        assert result.candidates[0].title == "Synthetic title"
        assert result.candidates[0].suggested_type == "Observation"
        assert len(digest) == 64
        assert "FORMAL LLM SYNTHESIS TASK" in prompt


def test_synthetic_local_ollama_failure_is_a_safe_stop():
    """An unreachable local endpoint raises LLMUnavailable, never a result."""
    class _Refusing:
        name = "ollama"
        endpoint = "http://127.0.0.1:1"
        requires_privacy_confirmation = False

        def complete(self, prompt):
            raise LLMUnavailable("llm_unavailable")

    with pytest.raises(LLMUnavailable):
        Synthesis(_Refusing()).synthesize()

