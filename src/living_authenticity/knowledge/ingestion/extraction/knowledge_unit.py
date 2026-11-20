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
    is_core: bool = False
    core_hash: str = ""
    core_source_path: str = ""

    def __init__(
        self,
        id: str,
        source: str,
        original_text: str,
        meaning: str,
        cleaned_text: str = "",
        normalized_text: str = "",
        context: str = "",
        proposed_type: str = "",
        position: int = 0,
        boundary_basis: str = "labeled_section",
        warnings: list[str] | None = None,
        input_id: str = "",
        input_hash: str = "",
        is_core: bool = False,
        core_hash: str = "",
        core_source_path: str = "",
    ) -> None:
        self.id = id
        self.source = source
        self.original_text = original_text
        self.meaning = meaning
        self.cleaned_text = cleaned_text
        self.normalized_text = normalized_text
        self.context = context
        self.proposed_type = proposed_type
        self.position = position
        self.boundary_basis = boundary_basis
        self.warnings = list(warnings) if warnings is not None else []
        self.input_id = input_id
        self.input_hash = input_hash
        self.is_core = is_core
        self.core_hash = core_hash
        self.core_source_path = core_source_path
