import hashlib
from abc import ABC, abstractmethod

from .knowledge_unit import KnowledgeUnit


def make_unit_id(source: str, position: int, text: str) -> str:
    """Build a deterministic unit id from stable inputs.

    No randomness is used, so extracting the same input with the same
    configuration always yields the same identifiers.
    """
    digest = hashlib.sha1(
        f"{source}:::{position}:::{text}".encode("utf-8")
    ).hexdigest()[:12]
    return f"ku-{digest}"


class KnowledgeUnitExtractor(ABC):
    """Base class for knowledge unit extractors.

    An extractor identifies meaningful, independent knowledge units from a
    single input while preserving semantic context and provenance.

    One input may yield zero, one, or many knowledge units.
    """

    @abstractmethod
    def extract(
        self,
        source: str,
        original_text: str,
        cleaned_text: str,
        normalized_text: str,
        parsed=None,
        parser=None,
        metadata: dict | None = None,
        input_provenance: object | None = None,
        **kwargs,
    ) -> list[KnowledgeUnit]:
        """Extract knowledge units from one input.

        Parameters
        ----------
        source:
            Identifier of the originating input (for example, the file path).
        original_text:
            The raw source text as read.
        cleaned_text:
            The cleaned/normalized-for-processing representation.
        normalized_text:
            The further normalized representation (may equal ``cleaned_text``
            when no additional normalization is configured).
        parsed:
            Optional parsed structure for the input, when available.
        parser:
            Optional parser instance, when available. Used by extractors that
            rely on parser-internal boundary detection.
        """
