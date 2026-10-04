"""Ollama provider tests over a fake local HTTP server (stdlib only)."""
import pytest

from src.living_authenticity.llm import LLMUnavailable, build_provider
from src.living_authenticity.llm import ollama as ollama_module
from tests.llm.fake_ollama_server import (
    FakeOllamaServer,
    install_no_proxy_opener,
    json_body,
)


def test_ollama_valid_malformed_and_timeout(monkeypatch):
    install_no_proxy_opener(monkeypatch)

    with FakeOllamaServer(json_body(title="t", body="b",
                                    suggested_type="",
                                    reason="r", uncertainty="u")) as server:
        provider = build_provider({"llm": {"provider": "ollama",
                                            "model": "m",
                                            "endpoint": server.endpoint,
                                            "timeout_seconds": 5}})
        assert provider.complete("hello")
        assert provider.complete("hello").startswith("{")


def test_ollama_invalid_json_and_timeout_fail_safely(monkeypatch):
    install_no_proxy_opener(monkeypatch)

    with FakeOllamaServer(b"not json") as server:
        provider = build_provider({"llm": {"provider": "ollama",
                                            "model": "m",
                                            "endpoint": server.endpoint,
                                            "timeout_seconds": 5}})
        with pytest.raises(LLMUnavailable):
            provider.complete("hello")

    with FakeOllamaServer(json_body(title="t", body="b",
                                    suggested_type="",
                                    reason="r", uncertainty="u"),
                          delay=2.0) as server:
        provider = ollama_module.OllamaProvider(
            model="m", endpoint=server.endpoint, timeout_seconds=1)
        with pytest.raises(LLMUnavailable):
            provider.complete("hello")


def test_ollama_remote_endpoint_requires_privacy_gate():
    provider = build_provider({"llm": {"provider": "ollama",
                                        "model": "m",
                                        "endpoint": "https://example.invalid"}})
    assert provider.requires_privacy_confirmation is True

