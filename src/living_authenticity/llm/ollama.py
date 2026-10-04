"""Local Ollama provider (standard library HTTP, no new dependency)."""
from .base import LLMProvider, LLMUnavailable
from .http import post_json
from .schema import candidate_response_schema

DEFAULT_ENDPOINT = "http://127.0.0.1:11434"


class OllamaProvider(LLMProvider):
    """Completion through a locally running Ollama server.

    The endpoint is configuration, so a "local" provider pointed at a
    remote host still triggers the endpoint-based privacy gate.
    """

    def __init__(self, model: str, endpoint: str = DEFAULT_ENDPOINT,
                 timeout_seconds: int = 120, temperature: float = 0.0,
                 max_candidates: int = 3) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if isinstance(timeout_seconds, bool) or not isinstance(
                timeout_seconds, int) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        self._model = model.strip()
        self._endpoint = endpoint.strip().rstrip("/")
        self._timeout = timeout_seconds
        self._temperature = float(temperature)
        self._max_candidates = max_candidates

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def temperature(self) -> float:
        return self._temperature

    def complete(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise LLMUnavailable("prompt must be a non-empty string")
        payload = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            # Constrain decoding to the synthesis contract so this server
            # cannot emit free text or a differently shaped object. The
            # schema is the shared contract, not an Ollama-specific one;
            # servers that ignore ``format`` simply decode unconstrained,
            # and the strict parser still decides.
            "format": candidate_response_schema(self._max_candidates),
            "options": {"temperature": self._temperature},
        }
        data = post_json(
            self._endpoint + "/api/generate", payload, self._timeout)
        if not isinstance(data, dict):
            raise LLMUnavailable("unexpected Ollama response shape")
        text = data.get("response")
        if not isinstance(text, str) or not text.strip():
            raise LLMUnavailable("Ollama returned an empty response")
        return text