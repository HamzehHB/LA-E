"""Reusable security utilities for the LivingAuthenticity Environment.

Default-deny by design. See ``SECURITY.md`` for the operational baseline and
``Coding-Agent-Access.md`` for the filesystem/data-access boundary.
"""

from .diagnostic import (
    safe_exception_diagnostic,
    sanitize_diagnostic_text,
    sanitize_path,
)
from .path_boundary import PathBoundary, PathOutsideBoundaryError
from .sensitive_data import (
    MACHINE_SPECIFIC_PATH_PREFIXES,
    SECRET_PATTERNS,
    contains_machine_specific_path,
    contains_secret,
    find_secrets,
)

__all__ = (
    "MACHINE_SPECIFIC_PATH_PREFIXES",
    "SECRET_PATTERNS",
    "PathBoundary",
    "PathOutsideBoundaryError",
    "contains_machine_specific_path",
    "contains_secret",
    "find_secrets",
    "safe_exception_diagnostic",
    "sanitize_diagnostic_text",
    "sanitize_path",
)
