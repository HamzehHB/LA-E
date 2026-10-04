"""Bounded read-only vault package (no writes, opt-in access only)."""
from .context import (
    discover_vault_files,
    read_vault_context,
    read_vault_units,
    resolve_vault_core_root,
)

__all__ = ["discover_vault_files", "read_vault_context", "read_vault_units",
           "resolve_vault_core_root"]
