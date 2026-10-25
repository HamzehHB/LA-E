"""One existing knowledge item retrieved as contextual evidence.

A candidate is evidence for later analysis stages. It is never a decision:
retrieving an item does not claim duplication, update, merge, novelty, a
relation, or any authorized action concerning that item.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievedCandidate:
    """An existing knowledge item relevant to one retrieval query.

    ``overlap_score`` is the count of distinct shared content tokens between
    the normalized query text and the normalized candidate text. It measures
    textual overlap only. It must never be interpreted as confidence,
    certainty, identity, authorization, or approval.
    """

    unit_id: str
    source: str
    position: int
    text: str
    matched_terms: tuple[str, ...] = ()
    overlap_score: int = 0
    retriever: str = ""
    evidence_basis: str = ""
    candidate_identity_key: str = ""
    vector_distance: float | None = None

    def __post_init__(self) -> None:
        if not self.candidate_identity_key:
            object.__setattr__(
                self,
                "candidate_identity_key",
                f"{self.source}:{self.position}:{self.unit_id}",
            )
