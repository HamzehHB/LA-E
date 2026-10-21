"""Vault identity computation and persistent index directory resolution.

Every authorized Vault receives a stable, non-sensitive identifier and a
dedicated persistent index directory within the configured vector database root.
Different Vaults never share database directories or index tables.
"""
import hashlib
from pathlib import Path

from src.living_authenticity.security.path_boundary import PathBoundary

__all__ = ("compute_vault_id", "resolve_vault_index_dir")


def compute_vault_id(vault_root: str) -> str:
    """Compute a stable, non-sensitive identity slug for one authorized Vault.

    Uses SHA-256 over the canonical resolved path. Private filesystem paths are
    never exposed in database names, events, or audit records.
    """
    if not isinstance(vault_root, str) or not vault_root.strip():
        raise ValueError("vault_root must be a non-empty string")
    resolved = str(Path(vault_root).resolve()).strip()
    # Normalize path separators and casing for stable cross-platform hashing
    normalized = resolved.replace("\\", "/").rstrip("/").lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
    return f"vault_{digest}"


def resolve_vault_index_dir(vector_db_root: str, vault_root: str) -> Path:
    """Return the dedicated persistent vector database directory for ``vault_root``.

    The directory resides strictly within ``vector_db_root``, guaranteeing
    physical isolation across different Vaults.
    """
    if not isinstance(vector_db_root, str) or not vector_db_root.strip():
        raise ValueError("vector_db_root must be a non-empty string")
    vault_id = compute_vault_id(vault_root)
    db_root_path = Path(vector_db_root).resolve()
    target_dir = db_root_path / vault_id
    PathBoundary(str(db_root_path)).validate(str(target_dir))
    return target_dir
