"""Provider-registry and contract tests (synthetic, no model needed).

These prove three project commitments:

* provider *and* model come from configuration only — no vendor or
  model name is hardcoded anywhere in the layer;
* locality/privacy-gating is derived from the resolved endpoint host,
  never from the provider name;
* every provider is one small implementation behind the same
  ``LLMProvider`` interface, so a new cloud request/response shape is
  a new module plus a dispatch branch, never a modification of an
  existing one.
"""
import ast
from pathlib import Path

import pytest

from Config.settings import ROOT as REPOSITORY_ROOT
from src.living_authenticity.llm import (
    KNOWN_PROVIDERS,
    LLMProvider,
    LLMUnavailable,
    build_provider,
    provider_limits,
)
from src.living_authenticity.llm import cloud as cloud_module
from src.living_authenticity.llm import ollama as ollama_module

LLM_PACKAGE = (REPOSITORY_ROOT / "src" / "living_authenticity" / "llm")

# Standard library (and intra-package) roots are the only imports a
# dependency-free provider layer may use. Anything else would mean a
# new runtime dependency shipped without a manifest change.
STDLIB_OR_INTERNAL = frozenset({
    "abc", "hashlib", "json", "os", "typing", "urllib",
    "base", "cloud", "context", "disabled", "http", "locality",
    "normalize", "ollama", "orchestration", "registry", "schema",
    "",  # relative imports (from . import ...)
})


def _config(**llm_overrides):
    return {"llm": llm_overrides}


def test_known_providers_are_generic_names_without_vendor_or_model():
    assert KNOWN_PROVIDERS == ("disabled", "ollama", "cloud")
    # Exact-name comparison only: "ollama" is the approved local-server
    # provider name and must not be flagged merely because it contains
    # the substring "llama". Vendor/model policing applies to whole
    # provider names, never to substrings.
    names = [name.lower() for name in KNOWN_PROVIDERS]
    for vendorish in ("openai", "anthropic", "claude", "gemini", "llama",
                      "mistral", "qwen"):
        assert vendorish not in names, vendorish


def test_disabled_is_the_safe_default():
    provider = build_provider(None)
    assert isinstance(provider, LLMProvider)
    assert provider.name == "disabled"
    assert provider.requires_privacy_confirmation is False
    with pytest.raises(LLMUnavailable):
        provider.complete("any prompt")


def test_model_comes_from_configuration_and_is_never_invented():
    provider = build_provider(_config(
        provider="ollama", model="my-chosen-model"))
    assert provider.model == "my-chosen-model"
    assert provider.name == "ollama"

    # No default model is silently substituted when config omits it.
    with pytest.raises(LLMUnavailable) as info:
        build_provider(_config(provider="ollama", model=""))
    assert "llm.model" in str(info.value)

    with pytest.raises(LLMUnavailable) as info:
        build_provider(_config(provider="cloud", model=""))
    assert "llm.model" in str(info.value)

    with pytest.raises(LLMUnavailable) as info:
        build_provider(_config(provider="cloud", model="m", endpoint=""))
    assert "llm.endpoint" in str(info.value)


def test_provider_names_carry_no_vendor_label():
    """The cloud implementation is generic: its name is "cloud"."""
    provider = build_provider(_config(
        provider="cloud", model="configured-model",
        endpoint="https://example.invalid"))
    assert provider.name == "cloud"
    assert provider.model == "configured-model"


def test_unknown_provider_fails_safely_with_known_names():
    with pytest.raises(LLMUnavailable) as info:
        build_provider(_config(provider="some_future_vendor", model="m",
                               endpoint="http://127.0.0.1:1"))
    message = str(info.value)
    assert "some_future_vendor" in message
    for known in KNOWN_PROVIDERS:
        assert known in message


def test_every_provider_is_one_instance_of_the_common_interface():
    providers = [
        build_provider(_config(provider="disabled")),
        build_provider(_config(provider="ollama", model="m")),
        build_provider(_config(provider="cloud", model="m",
                               endpoint="http://127.0.0.1:11434")),
    ]
    for provider in providers:
        assert isinstance(provider, LLMProvider)
        assert isinstance(provider.name, str) and provider.name
        assert isinstance(provider.model, str)
        assert isinstance(provider.endpoint, str)
        assert provider.is_local in (True, False)


def test_privacy_gate_depends_on_endpoint_not_provider_name():
    """provider: ollama with a non-local endpoint still transmits."""
    remote = build_provider(_config(
        provider="ollama", model="m",
        endpoint="http://remote-host.example:11434"))
    assert remote.name == "ollama"
    assert remote.is_local is False
    assert remote.requires_privacy_confirmation is True

    local = build_provider(_config(
        provider="ollama", model="m",
        endpoint="http://127.0.0.1:11434"))
    assert local.is_local is True
    assert local.requires_privacy_confirmation is False

    # The same rule applies to the cloud shape, whatever the host is.
    local_cloud = build_provider(_config(
        provider="cloud", model="m", endpoint="http://localhost:8080"))
    assert local_cloud.requires_privacy_confirmation is False
    remote_cloud = build_provider(_config(
        provider="cloud", model="m", endpoint="https://example.invalid"))
    assert remote_cloud.requires_privacy_confirmation is True


def test_provider_limits_come_from_config():
    assert provider_limits(_config(max_candidates=2,
                                   max_body_characters=500)) == (2, 500)
    assert provider_limits({}) == (3, 20000)
    assert provider_limits(_config(max_candidates=0)) == (3, 20000)


def _captured_request(monkeypatch, module, response):
    """Capture one provider request and serve a canned response."""
    seen = {}

    def _fake_post(url, payload, timeout_seconds, headers=None):
        seen.update(url=url, payload=payload, timeout=timeout_seconds,
                    headers=headers or {})
        return response

    monkeypatch.setattr(module, "post_json", _fake_post)
    return seen


def test_ollama_request_shape(monkeypatch):
    provider = build_provider(_config(provider="ollama", model="m"))
    seen = _captured_request(
        monkeypatch, ollama_module, {"response": "done"})
    assert provider.complete("hello") == "done"
    assert seen["url"].endswith("/api/generate")
    assert seen["payload"]["model"] == "m"
    assert seen["payload"]["stream"] is False
    assert "options" in seen["payload"]
    assert "Authorization" not in seen["headers"]


def test_ollama_binds_decoding_to_the_shared_contract_schema(monkeypatch):
    """The request constrains decoding to the same contract we validate."""
    from src.living_authenticity.llm import candidate_response_schema

    provider = build_provider(_config(provider="ollama", model="m",
                                      max_candidates=2))
    seen = _captured_request(monkeypatch, ollama_module, {"response": "{}"})
    provider.complete("hello")
    fmt = seen["payload"]["format"]
    assert fmt == candidate_response_schema(2)
    assert fmt["properties"]["candidates"]["maxItems"] == 2
    assert fmt["additionalProperties"] is False
    item = fmt["properties"]["candidates"]["items"]
    assert item["required"] == ["title", "body", "suggested_type",
                                "reason", "uncertainty"]
    assert item["additionalProperties"] is False
    assert "" in item["properties"]["suggested_type"]["enum"]


def test_cloud_request_shape_differs_from_ollama(monkeypatch):
    """Two shapes, one interface: different URL, payload, auth header."""
    provider = build_provider(_config(
        provider="cloud", model="m", endpoint="https://example.invalid",
        credential_env="SOME_TEST_CREDENTIAL"))
    monkeypatch.setenv("SOME_TEST_CREDENTIAL", "secret-value-not-for-logs")
    seen = _captured_request(
        monkeypatch, cloud_module, {"choices": [{"message": {"content": "ok"}}]})
    assert provider.complete("hello") == "ok"
    assert seen["url"].endswith("/v1/chat/completions")
    assert seen["payload"]["messages"][0]["content"] == "hello"
    assert seen["payload"]["model"] == "m"
    assert seen["headers"]["Authorization"] == "Bearer secret-value-not-for-logs"
    assert "stream" not in seen["payload"]


def test_cloud_malformed_or_empty_responses_fail_safely(monkeypatch):
    provider = build_provider(_config(
        provider="cloud", model="m", endpoint="https://example.invalid",
        credential_env="SOME_TEST_CREDENTIAL"))
    monkeypatch.setenv("SOME_TEST_CREDENTIAL", "s")

    for response in ({}, {"choices": []},
                     {"choices": [{"message": {"content": ""}}]}):
        monkeypatch.setattr(cloud_module, "post_json",
                            lambda *a, _r=response, **k: _r)
        with pytest.raises(LLMUnavailable):
            provider.complete("hello")


def test_cloud_missing_environment_credential_never_falls_back(monkeypatch):
    monkeypatch.delenv("SOME_TEST_CREDENTIAL", raising=False)
    provider = build_provider(_config(
        provider="cloud", model="m", endpoint="https://example.invalid",
        credential_env="SOME_TEST_CREDENTIAL"))
    calls = []

    monkeypatch.setattr(cloud_module, "post_json",
                        lambda *a, **k: calls.append(1))
    with pytest.raises(LLMUnavailable):
        provider.complete("hello")
    assert calls == [], "no request may leave the machine without a credential"


def test_provider_layer_adds_no_dependency():
    """The whole ``llm`` package imports only the standard library."""
    offenders = []
    for path in sorted(LLM_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if root not in STDLIB_OR_INTERNAL:
                    offenders.append(path.name + ":" + root)
    absolute_src_imports = []
    for path in sorted(LLM_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module or ""
                if module.startswith("src.") or module == "Config":
                    absolute_src_imports.append(path.name + ":" + module)
    assert absolute_src_imports == [], (
        "llm layer must use relative imports only: "
        f"{absolute_src_imports}")
    assert offenders == [], f"non-stdlib import in the LLM layer: {offenders}"
