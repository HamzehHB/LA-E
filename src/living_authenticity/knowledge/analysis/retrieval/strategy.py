"""User-selectable retrieval strategies and strategy registry.

Retrieval strategies govern how contextual evidence is selected from existing
knowledge. The strategy is validated before execution and recorded in audit
records. Silent fallback between strategies is forbidden: if a requested
strategy cannot be executed due to missing configuration, unvalidated resources,
or incompatible embeddings, the run fails safely and reports the cause.

Supported strategies in version 0.1:
* "lexical" (TokenOverlapRetriever): deterministic token overlap over existing
  knowledge units; requires zero embeddings and allocates no embedding cache.
* "semantic" (LanceDBVectorRetriever or compatible vector retriever): vector
  similarity search over an isolated persistent vector index; requires a loaded
  embedding service and allocates an isolated run-scoped temporary cache for
  current-input chunk embeddings.

Future-ready strategy (contract only):
* "hybrid": planned combination of lexical and semantic retrieval. It is defined
  in the registry as an extensible contract but marked unsupported in version 0.1;
  operational selection fails closed with an explicit UnsupportedStrategyError.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Final


class RetrievalStrategy(str, Enum):
    """Known retrieval strategies."""

    LEXICAL = "lexical"
    SEMANTIC = "semantic"
    HYBRID = "hybrid"


@dataclass(frozen=True)
class StrategyDefinition:
    """Metadata describing one retrieval strategy."""

    name: str
    is_supported: bool
    requires_embedding: bool
    description: str


class UnsupportedStrategyError(ValueError):
    """Raised when a requested retrieval strategy is unsupported or unavailable."""


class StrategyConfigurationError(ValueError):
    """Raised when a supported strategy lacks required runtime configuration."""


SUPPORTED_STRATEGIES: Final[dict[str, StrategyDefinition]] = {
    RetrievalStrategy.LEXICAL.value: StrategyDefinition(
        name=RetrievalStrategy.LEXICAL.value,
        is_supported=True,
        requires_embedding=False,
        description=(
            "Deterministic token-overlap retrieval; requires no embedding "
            "model or vector database."
        ),
    ),
    RetrievalStrategy.SEMANTIC.value: StrategyDefinition(
        name=RetrievalStrategy.SEMANTIC.value,
        is_supported=True,
        requires_embedding=True,
        description=(
            "Vector-similarity retrieval; uses isolated vector index and "
            "temporary input embedding cache."
        ),
    ),
    RetrievalStrategy.HYBRID.value: StrategyDefinition(
        name=RetrievalStrategy.HYBRID.value,
        is_supported=False,
        requires_embedding=True,
        description=(
            "Future-ready combined lexical and semantic candidate retrieval "
            "(planned; not operational in 0.1)."
        ),
    ),
}


class RetrievalStrategyRegistry:
    """Registry of retrieval strategy definitions and validation."""

    def __init__(
        self, definitions: dict[str, StrategyDefinition] | None = None
    ) -> None:
        self._definitions = (
            dict(definitions)
            if definitions is not None
            else dict(SUPPORTED_STRATEGIES)
        )

    def get(self, name: str) -> StrategyDefinition | None:
        if not isinstance(name, str):
            return None
        return self._definitions.get(name.strip().lower())

    def validate(self, name: str) -> StrategyDefinition:
        """Validate that ``name`` is an operational, supported strategy.

        Raises:
            UnsupportedStrategyError: if ``name`` is unknown or is a planned future
                strategy that is not yet operational (e.g. "hybrid").
        """
        if not isinstance(name, str) or not name.strip():
            raise UnsupportedStrategyError(
                "retrieval strategy name must be a non-empty string"
            )
        norm = name.strip().lower()
        defn = self._definitions.get(norm)
        if defn is None:
            supported = [k for k, v in self._definitions.items() if v.is_supported]
            raise UnsupportedStrategyError(
                f"unsupported retrieval strategy '{name}'; available supported strategies: {', '.join(supported)}"
            )
        if not defn.is_supported:
            if norm == RetrievalStrategy.HYBRID.value:
                raise UnsupportedStrategyError(
                    "Hybrid retrieval is a planned future strategy and is not supported in version 0.1."
                )
            raise UnsupportedStrategyError(
                f"retrieval strategy '{name}' is not currently supported"
            )
        return defn

    def list_supported(self) -> tuple[str, ...]:
        return tuple(k for k, v in self._definitions.items() if v.is_supported)
