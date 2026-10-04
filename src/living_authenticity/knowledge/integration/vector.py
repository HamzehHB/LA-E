"""Bounded vector-store composition for local-integration runs.

Composition only, no new authority. This module wires the existing
embedding service and the existing LanceDB manager into the existing
retrieval contract; it introduces no new pipeline and no new write
authority for knowledge artifacts.

Writes here are vector-index operations on an explicitly supplied,
isolated vector database — a retrieval cache, never an authoritative
knowledge store. Knowledge artifacts still reach the world only
through the existing ``ControlledExecutor`` boundary.
"""
from pathlib import Path

from Config.settings import ROOT as REPOSITORY_ROOT
from Config.settings import load_paths
from src.living_authenticity.database.lancedb_manager import LanceDBManager
from src.living_authenticity.embedding.service import EmbeddingService
from src.living_authenticity.knowledge.analysis.retrieval.lancedb_retriever import (
    LanceDBVectorRetriever,
)
from src.living_authenticity.security.path_boundary import PathBoundary

from .guard import _ALLOWED_SOURCE_SUFFIXES

# Configuration key holding the production vector database. The isolated
# validation database must never resolve to this location.
PRODUCTION_VECTOR_DB_KEY = "vector_db.lancedb"


def _lookup(data: dict, dotted_key: str):
    """Walk nested mappings for one dotted key; return None when absent."""
    value = data
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def resolve_production_vector_db(config=None) -> str:
    """Return the configured production vector database path, or ``""``."""
    data = config if isinstance(config, dict) else load_paths()
    value = _lookup(data, PRODUCTION_VECTOR_DB_KEY)
    return value.strip() if isinstance(value, str) else ""


def check_vector_store_eligible(db_path, production_vector_db="",
                               vault_root="") -> tuple:
    """Return ``(eligible, reason, check)`` for one vector store candidate.

    The candidate must be explicitly supplied, outside the repository,
    and never identical to (or inside) the configured production vector
    database. As a non-authoritative retrieval index it must also stay
    outside the configured Obsidian vault root, so index artifacts can
    never be written into authoritative notes. The directory may or may
    not exist yet: LanceDB creates an empty database on first connect.
    When it exists, it must be a real directory and not a symlink.
    """
    if not isinstance(db_path, str) or not db_path.strip():
        return (False, "vector store path missing", "vector_db")
    candidate = Path(db_path)
    if candidate.exists():
        if candidate.is_symlink():
            return (False, "vector store is a symlink", "vector_db")
        if not candidate.is_dir():
            return (False, "vector store is not a directory", "vector_db")
    if PathBoundary(str(REPOSITORY_ROOT)).is_allowed(db_path):
        return (False, "vector store inside repository workspace", "vector_db")
    production = (
        production_vector_db.strip()
        if isinstance(production_vector_db, str) else ""
    )
    if production and PathBoundary(production).is_allowed(db_path):
        return (False, "vector store is the production vector database",
                "vector_db")
    vault = vault_root.strip() if isinstance(vault_root, str) else ""
    if vault and PathBoundary(vault).is_allowed(db_path):
        return (False, "vector store inside the Obsidian vault",
                "vector_db")
    return (True, "", "")


def build_vector_components(vector_db_path, embedder=None, model_path=None,
                            max_candidates: int = 5):
    """Build the isolated vector retrieval triple for one run.

    Returns ``(embedder, store, retriever)``. When ``embedder`` is not
    supplied, the real local embedding service is loaded from
    ``model_path``; tests inject a deterministic stub embedder instead
    so the tracked suite needs no model files.
    """
    if embedder is not None and hasattr(embedder, "embed"):
        service = embedder
    else:
        if not isinstance(model_path, str) or not model_path.strip():
            raise ValueError("model_path is required without an embedder")
        service = EmbeddingService(model_path)
    store = LanceDBManager(db_path=vector_db_path)
    store.create_knowledge_vector_table()
    retriever = LanceDBVectorRetriever(
        embedding_service=service, db_manager=store,
        max_candidates=max_candidates)
    return (service, store, retriever)
