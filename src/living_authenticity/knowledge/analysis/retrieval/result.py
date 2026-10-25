"""Outcome of retrieving existing-knowledge candidates for one query unit.

Retrieval is an analytical evidence-gathering stage. A result records what
was queried, which existing items were found, and the limits of the search.
It proposes nothing, decides nothing, and authorizes nothing.
"""

from dataclasses import dataclass, field

from .candidate import RetrievedCandidate


@dataclass(frozen=True)
class RetrievalResult:
    """Evidence-only retrieval outcome for one query knowledge unit.

    ``query_terms`` preserves the normalized query tokens so later stages can
    see exactly what was searched for. ``corpus_size`` is the number of collection
    items examined (or None if the collection size could not be determined).
    ``note`` records relevant limitations or uncertainty, for example when the query
    has no analyzable content, nothing matched, or corpus size is unknown.
    """

    query_unit_id: str
    query_source: str
    query_position: int
    strategy: str = ""
    candidates: tuple[RetrievedCandidate, ...] = ()
    corpus_size: int | None = None
    query_terms: tuple[str, ...] = ()
    note: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def has_candidates(self) -> bool:
        """True when at least one existing item was retrieved."""
        return bool(self.candidates)
