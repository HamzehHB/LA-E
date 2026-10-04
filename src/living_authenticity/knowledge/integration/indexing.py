"""Explicit bounded corpus ingestion and isolated vector indexing.

Reuse only: ``pipeline.ingestion`` (existing ingestion stage),
``store`` (existing LanceDB manager), and ``embedder`` (existing
embedding service). No stage logic is duplicated here.
"""
from pathlib import Path

from src.living_authenticity.security.path_boundary import PathBoundary

from .guard import _ALLOWED_SOURCE_SUFFIXES


__all__ = ("index_corpus", "index_units", "ingest_units")


def _unit_text(unit) -> str:
    """Best available text of an extracted unit for embedding."""
    return (
        unit.cleaned_text
        or unit.normalized_text
        or unit.meaning
        or unit.original_text
        or ""
    )


def _bounded_files(directory, max_files: int) -> tuple:
    """Return ``(chosen, skipped)`` for one explicit directory."""
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("directory must be a non-empty string")
    if not isinstance(max_files, int) or isinstance(max_files, bool):
        raise ValueError("max_files must be a positive integer")
    if max_files < 1:
        raise ValueError("max_files must be a positive integer")
    path = Path(directory)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("directory must be an existing directory")
    try:
        boundary = PathBoundary(directory)
    except Exception as exc:
        raise ValueError("directory invalid") from exc
    try:
        children = sorted(path.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        raise ValueError("directory unreadable") from exc
    chosen = []
    skipped = 0
    for child in children:
        if child.suffix.lower() not in _ALLOWED_SOURCE_SUFFIXES:
            continue
        if len(chosen) >= max_files:
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
        chosen.append(child)
    return (chosen, skipped)


def ingest_units(directory, pipeline, max_files: int = 1) -> list:
    """Ingest one explicit directory into a list of knowledge units.

    Used for explicit corpus and explicit Core inputs so later stages
    receive designated, reviewed data without any filesystem discovery.
    """
    units: list = []
    chosen, _skipped = _bounded_files(directory, max_files)
    for child in chosen:
        try:
            ingestion = pipeline.ingestion.ingest(str(child))
        except Exception:
            continue
        units.extend(ingestion.knowledge_units)
    return units


def _existing_vector_count(store) -> int:
    """Return the number of rows already stored, or 0 when unreadable."""
    try:
        return len(store.show_all())
    except Exception:
        return 0


def index_units(units, embedder, store) -> tuple:
    """Index already-ingested units into the isolated vector store.

    Vault-aware helper: callers ingest vault notes read-only via
    ``vault.read_vault_units`` (or any explicit corpus) and index the
    resulting units here. Returns ``(units_indexed, vectors_stored)``.

    Indexing is refused when the store is not empty, so repeated runs
    cannot silently duplicate vectors. This is indexing, never
    authorization.
    """
    if embedder is None or not hasattr(embedder, "embed_batch"):
        raise ValueError("embedder must expose embed_batch(texts)")
    existing = _existing_vector_count(store)
    if existing:
        raise ValueError(
            "the isolated vector store already holds " + str(existing)
            + " vectors; refusing to index again so retrieval evidence "
            "stays reproducible. Reuse the existing index, or supply a "
            "fresh empty vector directory."
        )
    ready = [unit for unit in units if _unit_text(unit).strip()]
    vectors = embedder.embed_batch([_unit_text(unit) for unit in ready])
    count = 0
    for unit, vector in zip(ready, vectors):
        store.store(
            _unit_text(unit), vector,
            {"source": unit.source, "position": unit.position},
        )
        count += 1
    return (count, count)


def index_corpus(corpus_root, pipeline, embedder, store,
                 max_files: int = 1) -> tuple:
    """Index one explicit corpus directory into the isolated vector store.

    Each file is ingested with the existing ingestion stage
    (``pipeline.ingestion``), its extracted units are embedded in one
    batch, and every embedding is stored with source and position
    provenance. Returns ``(files_indexed, units_indexed,
    vectors_stored, files_skipped)``.

    Indexing is refused when the store is not empty. Storing the same
    corpus twice would accumulate duplicate rows, which would change the
    retrieval evidence on later runs while the inputs stayed identical —
    a provenance and repeatability hazard. The operator either reuses the
    existing index (omit the corpus) or points at a fresh, empty,
    explicitly supplied vector directory.

    This is indexing, not authorization: storing a vector changes no
    authoritative knowledge and approves nothing.
    """
    if embedder is None or not hasattr(embedder, "embed_batch"):
        raise ValueError("embedder must expose embed_batch(texts)")
    existing = _existing_vector_count(store)
    if existing:
        raise ValueError(
            "the isolated vector store already holds " + str(existing)
            + " vectors; refusing to index again so retrieval evidence "
            "stays reproducible. Reuse the existing index, or supply a "
            "fresh empty vector directory."
        )
    files_indexed = 0
    units_indexed = 0
    vectors_stored = 0
    files_skipped = 0
    chosen, over = _bounded_files(corpus_root, max_files)
    files_skipped += over
    for child in chosen:
        try:
            ingestion = pipeline.ingestion.ingest(str(child))
        except Exception:
            files_skipped += 1
            continue
        units = [
            unit for unit in ingestion.knowledge_units
            if _unit_text(unit).strip()
        ]
        if not units:
            files_skipped += 1
            continue
        files_indexed += 1
        vectors = embedder.embed_batch(
            [_unit_text(unit) for unit in units])
        for unit, vector in zip(units, vectors):
            store.store(
                _unit_text(unit), vector,
                {"source": unit.source, "position": unit.position},
            )
            units_indexed += 1
            vectors_stored += 1
    return (files_indexed, units_indexed, vectors_stored, files_skipped)
