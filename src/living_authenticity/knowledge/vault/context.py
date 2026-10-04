"""Bounded read-only vault access: discovery, context, and retrieval units.

Everything here is strictly read-only. The vault is never written,
renamed, deleted, or mutated, and every path is confined to the
configured vault root by ``PathBoundary``.
"""
from pathlib import Path

import yaml

from src.living_authenticity.security.path_boundary import PathBoundary

_MAX_FILES = 10
_MAX_BYTES_PER_FILE = 131072
_MAX_TOTAL_BYTES = 2097152
_ALLOWED_SUFFIXES = frozenset({".md"})

# ``Knowledge-Schema.yaml`` (tracked, machine-independent) is the single
# source of truth for which vault folder holds Core knowledge. It is read
# at runtime rather than hard-coded, so no folder name can become a
# competing authority and another Vault following the same schema works
# unchanged. Depth: .../src/living_authenticity/knowledge/vault/context.py
_SCHEMA_PATH = Path(__file__).resolve().parents[4] / "Knowledge-Schema.yaml"


def _bounded_vault_files(root, max_files, max_bytes_per_file, max_total_bytes,
                         boundary):
    """Return ``(files, skipped)`` for eligible notes under ``root``.

    Recursively discovers Markdown notes (the real vault stores them in
    nested folders such as ``01 Core/`` and ``02 Observation/``), applying
    the count, per-file size, and total-size bounds in deterministic
    sorted order. Anything rejected is counted in ``skipped``.
    """
    files: list = []
    skipped = 0
    total = 0
    try:
        candidates = sorted(root.rglob("*.md"), key=lambda p: str(p))
    except OSError:
        return ([], 1)
    for child in candidates:
        if len(files) >= max_files or total >= max_total_bytes:
            skipped += 1
            continue
        if child.suffix.lower() not in _ALLOWED_SUFFIXES:
            skipped += 1
            continue
        if child.is_symlink() or not child.is_file():
            skipped += 1
            continue
        try:
            boundary.validate(str(child))
        except Exception:
            skipped += 1
            continue
        try:
            size = child.stat().st_size
        except OSError:
            skipped += 1
            continue
        if size > max_bytes_per_file:
            skipped += 1
            continue
        if total + size > max_total_bytes:
            skipped += 1
            continue
        total += size
        files.append(child)
    return (files, skipped)


def discover_vault_files(vault_root, max_files=_MAX_FILES,
                         max_bytes_per_file=_MAX_BYTES_PER_FILE,
                         max_total_bytes=_MAX_TOTAL_BYTES):
    """Return ``(files, skipped)`` for the configured vault, read-only."""
    if not isinstance(vault_root, str) or not vault_root.strip():
        return ([], 1)
    root = Path(vault_root)
    if root.is_symlink() or not root.is_dir():
        return ([], 1)
    try:
        boundary = PathBoundary(vault_root)
    except Exception:
        return ([], 1)
    return _bounded_vault_files(
        root, max_files, max_bytes_per_file, max_total_bytes, boundary)


def read_vault_context(vault_root: str, max_files=_MAX_FILES,
                       max_bytes_per_file=_MAX_BYTES_PER_FILE,
                       max_total_bytes=_MAX_TOTAL_BYTES):
    """Return ``(excerpts, skipped)`` without writing anywhere."""
    excerpts: list = []
    if not isinstance(vault_root, str) or not vault_root.strip():
        return ([], 1)
    root = Path(vault_root)
    if root.is_symlink() or not root.is_dir():
        return ([], 1)
    try:
        boundary = PathBoundary(vault_root)
    except Exception:
        return ([], 1)
    files, skipped = _bounded_vault_files(
        root, max_files, max_bytes_per_file, max_total_bytes, boundary)
    total = 0
    for child in files:
        try:
            text = child.read_text(encoding="utf-8")
        except (OSError, ValueError, UnicodeError):
            skipped += 1
            continue
        data = text.encode("utf-8")
        if len(data) > max_bytes_per_file or total + len(data) > max_total_bytes:
            skipped += 1
            continue
        total += len(data)
        excerpts.append({"source": str(child), "excerpt": text})
    return (excerpts, skipped)


def _schema_core_folder() -> str:
    """Return the authoritative Core folder name from ``Knowledge-Schema.yaml``.

    ``folder_structure`` is the single source of truth: the Core folder is
    the entry whose ``canonical_type`` is ``"Core"``. Reading it (same
    ``yaml.safe_load`` pattern as ``governance/revalidation/schema_version.py``)
    keeps the folder definition out of code, so no duplicated literal or
    machine-specific value can become a competing authority. Returns ``""``
    when the schema cannot be read, so callers report no Core rather than
    guessing.
    """
    try:
        data = yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return ""
    structure = data.get("folder_structure") if isinstance(data, dict) else None
    if not isinstance(structure, dict):
        return ""
    for folder, spec in structure.items():
        if isinstance(spec, dict) and spec.get("canonical_type") == "Core":
            return str(folder)
    return ""


def resolve_vault_core_root(vault_root):
    """Return the configured vault's Core folder path, or ``""``.

    The Core folder name comes from the schema's ``folder_structure`` (the
    authoritative definition), and the vault root comes from configuration
    (``obsidian.vault``), so nothing here depends on a private path or a
    hard-coded folder name. The folder is returned only when it exists, is
    a real directory, and resolves inside the configured vault root;
    otherwise ``""`` is returned so callers fall back to no Core (or an
    explicitly supplied ``--core-root``). Read-only.
    """
    if not isinstance(vault_root, str) or not vault_root.strip():
        return ""
    root = Path(vault_root)
    if root.is_symlink() or not root.is_dir():
        return ""
    core_folder = _schema_core_folder()
    if not core_folder:
        return ""
    candidate = root / core_folder
    try:
        if candidate.is_symlink() or not candidate.is_dir():
            return ""
        PathBoundary(str(root)).validate(str(candidate))
    except Exception:
        return ""
    return str(candidate)


def read_vault_units(vault_root, pipeline, max_files=_MAX_FILES,
                     max_bytes_per_file=_MAX_BYTES_PER_FILE,
                     max_total_bytes=_MAX_TOTAL_BYTES):
    """Return ``(units, files_discovered, files_skipped)``, read-only.

    Notes are parsed through the existing ingestion stage so vault
    knowledge is represented exactly like any other source. The vault is
    only read; nothing is written back.
    """
    files, skipped = discover_vault_files(
        vault_root, max_files, max_bytes_per_file, max_total_bytes)
    units: list = []
    for child in files:
        try:
            ingestion = pipeline.ingestion.ingest(str(child))
        except Exception:
            skipped += 1
            continue
        units.extend(ingestion.knowledge_units)
    return (units, len(files), skipped)


__all__ = ("discover_vault_files", "read_vault_context", "read_vault_units",
           "resolve_vault_core_root")

