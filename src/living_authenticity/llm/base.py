"""Provider-independent LLM layer contract.

The LLM is a formal workflow component between Knowledge Filter and
human review: it receives the real structured pipeline results and
produces candidate outputs for review. It never decides, approves,
executes, authorizes a destination, or changes governance.

``LLM output != authorization`` — suggested actions, confidence, and
reasoning from any provider are analysis material only.
"""
from abc import ABC, abstractmethod


class LLMUnavailable(RuntimeError):
    """The provider could not produce a usable completion.

    Callers must treat this as a safe failure: the formal LLM stage
    produces no candidates for the affected unit, nothing is displayed
    for human review, no approval is requested, and nothing executes.
    The unit stops with an explicit audited safe-failure reason
    (``llm_unavailable``, ``llm_timeout``, ``llm_invalid_output``).
    A disabled or unavailable provider never turns the formal LLM
    stage into an invisible bypass.
    """


class LLMProvider(ABC):
    """One replaceable implementation behind the formal LLM layer."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable provider identifier recorded on synthesis results."""
        raise NotImplementedError

    @property
    @abstractmethod
    def model(self) -> str:
        """Configured model name (never a filesystem path)."""
        raise NotImplementedError

    @property
    @abstractmethod
    def endpoint(self) -> str:
        """Configured endpoint this provider would transmit to."""
        raise NotImplementedError

    @property
    def is_local(self) -> bool:
        """Locality is resolved from the endpoint host, never the name."""
        from .locality import is_local_endpoint

        return is_local_endpoint(self.endpoint)

    @property
    def requires_privacy_confirmation(self) -> bool:
        """True when data would leave the machine (endpoint-based)."""
        return not self.is_local

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """Return raw model text for ``prompt`` or raise LLMUnavailable."""
        raise NotImplementedError
