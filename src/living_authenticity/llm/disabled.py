"""Disabled provider: a valid, safe, network-free configuration."""
from .base import LLMProvider, LLMUnavailable


class DisabledProvider(LLMProvider):
    """No network, no model call; every completion attempt fails safely.

    Being disabled is an explicit configuration state, not a broken
    architecture: the mandatory synthesis step still runs and then stops
    safely with an explicit reason. It produces no candidates, so no
    candidate is displayed, no approval is requested, and nothing
    executes.
    """

    def __init__(self, model: str = "") -> None:
        if not isinstance(model, str):
            raise TypeError("model must be a string")
        self._model = model

    @property
    def name(self) -> str:
        return "disabled"

    @property
    def model(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return ""

    @property
    def is_local(self) -> bool:
        return True

    def complete(self, prompt: str) -> str:
        raise LLMUnavailable("LLM provider is disabled by configuration")
