"""Provider selection for the formal LLM layer.

Switching provider *and* model is a configuration edit only
(``Config/llm.local.yaml``): neither is hardcoded in code, and no
vendor-specific logic ever enters the pipeline. The registry maps the
configured ``provider:`` name to a concrete implementation behind the
common :class:`~src.living_authenticity.llm.base.LLMProvider`
interface.

Adding a different cloud API shape later means: add one small module
under ``llm/`` implementing the same interface, then add one dispatch
branch in ``build_provider`` below. The shared interface and existing
provider modules are never modified.

Known providers today:

* ``disabled``  — safe, network-free configuration (default).
* ``ollama``    — local Ollama server (stdlib HTTP).
* ``cloud``     — one example cloud request/response shape (stdlib
  HTTP); its endpoint may be any provider's endpoint matching that
  shape. Different shapes get their own module later.
"""
from .base import LLMProvider, LLMUnavailable
from .cloud import CloudProvider
from .disabled import DisabledProvider
from .ollama import DEFAULT_ENDPOINT, OllamaProvider

KNOWN_PROVIDERS = ("disabled", "ollama", "cloud")


def _section(config) -> dict:
    if not isinstance(config, dict):
        return {}
    candidate = config.get("llm")
    return candidate if isinstance(candidate, dict) else {}


def _text(section: dict, key: str) -> str:
    value = section.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _positive_int(section: dict, key: str, default: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return default
    return value


def build_provider(config: dict | None = None) -> LLMProvider:
    """Return the configured provider implementation.

    ``config`` is the mapping produced by ``Config.settings.load_llm``.
    An absent or unreadable section yields the safe ``disabled``
    provider: the formal LLM stage still runs, attempts no network
    call, produces no candidates, and the affected unit stops safely
    with an explicit reason instead of proceeding without LLM
    synthesis. There is no bypass path around the formal stage.

    Model names are never invented here: a provider that requires one
    fails safely when the configuration does not supply it.
    """
    section = _section(config)
    provider = _text(section, "provider").lower() or "disabled"
    model = _text(section, "model")
    endpoint = _text(section, "endpoint")
    credential_env = _text(section, "credential_env")
    timeout = _positive_int(section, "timeout_seconds", 120)
    max_tokens = _positive_int(section, "max_tokens", 1024)
    max_candidates = _positive_int(section, "max_candidates", 3)

    if provider == "disabled":
        return DisabledProvider(model=model)
    if provider == "ollama":
        if not model:
            raise LLMUnavailable(
                "llm.model must be set in the LLM configuration for the "
                "ollama provider (no default model is invented)")
        return OllamaProvider(
            model=model,
            endpoint=endpoint or DEFAULT_ENDPOINT,
            timeout_seconds=timeout,
            max_candidates=max_candidates,
        )
    if provider == "cloud":
        if not model:
            raise LLMUnavailable(
                "llm.model must be set in the LLM configuration for the "
                "cloud provider (no default model is invented)")
        if not endpoint:
            raise LLMUnavailable(
                "llm.endpoint must be set in the LLM configuration for "
                "the cloud provider")
        return CloudProvider(
            model=model, endpoint=endpoint,
            credential_env=credential_env,
            timeout_seconds=timeout, max_tokens=max_tokens,
        )
    raise LLMUnavailable(
        "unknown llm.provider: " + provider
        + " (known: " + ", ".join(KNOWN_PROVIDERS) + ")")


def provider_limits(section: dict | None = None) -> tuple:
    """Return ``(max_candidates, max_body_characters)`` from config."""
    data = _section(section) if isinstance(section, dict) else {}
    if not isinstance(data, dict):
        data = {}
    return (_positive_int(data, "max_candidates", 3),
            _positive_int(data, "max_body_characters", 20000))