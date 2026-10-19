from dataclasses import dataclass, field


@dataclass
class KnowledgeUnit:
    """One meaningful, independently analyzable knowledge unit.

    This is an analytical representation only. It is not authoritative
    knowledge and does not modify any authoritative state.

    ``proposed_type`` defaults to an empty string, representing an
    unresolved type. Type assignment (Classification) belongs to a later
    stage; the extraction stage performs extraction only.
    """

    id: str
    source: str
    original_text: str
    meaning: str
    cleaned_text: str = ""
    normalized_text: str = ""
    context: str = ""
    proposed_type: str = ""
    position: int = 0
    boundary_basis: str = "labeled_section"
    warnings: list[str] = field(default_factory=list)
    input_id: str = ""
    input_hash: str = ""
