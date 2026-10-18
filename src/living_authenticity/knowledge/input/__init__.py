"""Input stage contract and processing exports."""
from .processor import (
    ACCEPTED_SOURCE_TYPES,
    MAX_TEXT_BYTES,
    InputProvenance,
    InputValidationResult,
    process_input,
    record_input_failure_audit,
)

__all__ = [
    "ACCEPTED_SOURCE_TYPES",
    "MAX_TEXT_BYTES",
    "InputProvenance",
    "InputValidationResult",
    "process_input",
    "record_input_failure_audit",
]

