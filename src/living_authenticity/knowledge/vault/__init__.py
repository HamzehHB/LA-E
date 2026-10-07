"""Bounded read-only vault package (no writes, opt-in access only)."""
from .context import (
    diff_manifests,
    discover_vault_files,
    file_identity,
    read_vault_context,
    read_vault_units,
    resolve_vault_core_root,
    snapshot_vault,
    verify_index_freshness,
)

__all__ = ["diff_manifests", "discover_vault_files", "file_identity",
           "read_vault_context", "read_vault_units",
           "resolve_vault_core_root", "snapshot_vault",
           "verify_index_freshness"]
