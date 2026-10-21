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
from .identity import compute_vault_id, resolve_vault_index_dir
from .sync import (
    VaultSyncError,
    VaultSyncMonitor,
    VaultSyncSession,
)

__all__ = [
    "diff_manifests",
    "discover_vault_files",
    "file_identity",
    "read_vault_context",
    "read_vault_units",
    "resolve_vault_core_root",
    "snapshot_vault",
    "verify_index_freshness",
    "compute_vault_id",
    "resolve_vault_index_dir",
    "VaultSyncSession",
    "VaultSyncMonitor",
    "VaultSyncError",
]
