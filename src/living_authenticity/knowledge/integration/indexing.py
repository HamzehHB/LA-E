"""Explicit bounded corpus ingestion and isolated vector indexing.

Reuse only: ``pipeline.ingestion`` (existing ingestion stage),
``store`` (existing LanceDB manager), and ``embedder`` (existing
embedding service). No stage logic is duplicated here.
"""
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Protocol, runtime_checkable

import inspect


def _determine_store_caller(store):
    """Determine Store API capability once prior to mutation loop to avoid unsafe TypeError retries."""
    store_fn = getattr(store, "store", None)
    if not callable(store_fn):
        raise TypeError("store must provide a callable store() method")
    try:
        sig = inspect.signature(store_fn)
        params = sig.parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        has_val_core = accepts_kwargs or "validated_core" in params
        has_rec_id = accepts_kwargs or "record_id" in params
    except (ValueError, TypeError):
        has_val_core = True
        has_rec_id = True

    if has_val_core and has_rec_id:
        return lambda text, vec, meta, rec_id, val_core: store_fn(
            text, vec, meta, record_id=rec_id, validated_core=val_core
        )
    elif has_rec_id:
        return lambda text, vec, meta, rec_id, val_core: store_fn(
            text, vec, meta, record_id=rec_id
        )
    else:
        return lambda text, vec, meta, rec_id, val_core: store_fn(
            text, vec, meta
        )


from src.living_authenticity.security.path_boundary import PathBoundary

from .guard import _ALLOWED_SOURCE_SUFFIXES


__all__ = ("index_corpus", "index_units", "ingest_units",
           "synchronize_index", "describe_index_state")


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


def index_units(units, embedder, store, validated_core=None) -> tuple:
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

    prev_meta = {}
    # Invalidate any existing Fresh status prior to modifying vectors
    if hasattr(store, "get_metadata") and hasattr(store, "save_metadata"):
        try:
            prev = store.get_metadata()
            if prev and isinstance(prev, dict):
                prev_meta = dict(prev)
                if prev.get("status") == "fresh":
                    stale_meta = dict(prev)
                    stale_meta["status"] = "unverified"
                    stale_meta["unverified"] = True
                    stale_meta["unverified_reason"] = "modified_by_generic_indexing"
                    store.save_metadata(stale_meta)
            else:
                store.save_metadata({
                    "status": "unverified",
                    "unverified": True,
                    "unverified_reason": "indexing_in_progress",
                    "indexing_source": "index_units",
                })
        except Exception as exc:
            from src.living_authenticity.database.lancedb_manager import DatabaseOperationError
            raise DatabaseOperationError(
                f"Failed invalidating Fresh status prior to vector mutation: {exc}"
            ) from exc

    ready = [unit for unit in units if _unit_text(unit).strip()]
    vectors = embedder.embed_batch([_unit_text(unit) for unit in ready])
    store_caller = _determine_store_caller(store)
    count = 0
    for unit, vector in zip(ready, vectors):
        # Strict verifiable Core provenance:
        # A normal note must NEVER become a Core vector merely by carrying Core-like metadata.
        # If no authoritative ValidatedCore context is supplied, every unit is indexed as ordinary knowledge.
        # If a validated Core context is supplied, verify each unit against that context.
        # Only genuinely validated Core-derived units may be stored with is_core=True and active Core hash.
        is_core_val = False
        core_hash_val = ""
        record_id = None
        if validated_core is not None:
            from src.living_authenticity.knowledge.vault.core_validator import (
                is_validated_core_unit,
                compute_core_record_id,
            )
            if is_validated_core_unit(unit, context=validated_core):
                val_ctx = getattr(validated_core, "validation_context", None)
                is_core_val = True
                core_hash_val = val_ctx.core_hash if val_ctx else getattr(validated_core, "sha256", "")
                record_id = compute_core_record_id(unit, validated_core)

        metadata = {
            "source": unit.source,
            "position": unit.position,
            "is_core": is_core_val,
            "core_hash": core_hash_val,
        }
        if record_id:
            metadata["record_id"] = record_id

        store_caller(
            _unit_text(unit), vector, metadata, record_id, validated_core
        )
        count += 1
    if hasattr(store, "save_metadata") and callable(store.save_metadata):
        val_ctx = getattr(validated_core, "validation_context", None)
        active_hash = val_ctx.core_hash if val_ctx else getattr(validated_core, "sha256", "")
        prev_v = prev_meta.get("indexed_vectors", 0)
        updated_meta = dict(prev_meta)
        updated_meta.update({
            "status": "unverified",
            "unverified": True,
            "unverified_reason": "modified_by_generic_indexing",
            "indexing_source": "index_units",
            "indexed_vectors": count if not prev_v else prev_v + count,
        })
        if active_hash:
            updated_meta["core_hash"] = active_hash
        store.save_metadata(updated_meta)
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
    prev_meta = {}
    # Invalidate any existing Fresh status prior to modifying vectors
    if hasattr(store, "get_metadata") and hasattr(store, "save_metadata"):
        try:
            prev = store.get_metadata()
            if prev and isinstance(prev, dict):
                prev_meta = dict(prev)
                if prev.get("status") == "fresh":
                    stale_meta = dict(prev)
                    stale_meta["status"] = "unverified"
                    stale_meta["unverified"] = True
                    stale_meta["unverified_reason"] = "modified_by_generic_indexing"
                    store.save_metadata(stale_meta)
            else:
                store.save_metadata({
                    "status": "unverified",
                    "unverified": True,
                    "unverified_reason": "indexing_in_progress",
                    "indexing_source": "index_corpus",
                })
        except Exception as exc:
            from src.living_authenticity.database.lancedb_manager import DatabaseOperationError
            raise DatabaseOperationError(
                f"Failed invalidating Fresh status prior to vector mutation: {exc}"
            ) from exc

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
                {
                    "source": unit.source,
                    "position": unit.position,
                    "is_core": False,
                    "core_hash": "",
                },
            )
            units_indexed += 1
            vectors_stored += 1
    if hasattr(store, "save_metadata") and callable(store.save_metadata):
        from src.living_authenticity.knowledge.vault.context import snapshot_vault
        try:
            snap = snapshot_vault(corpus_root, max_files=max_files)
        except Exception as exc:
            raise RuntimeError(
                f"Failed snapshotting corpus '{corpus_root}' during indexing: {exc}"
            ) from exc
        prev_v = prev_meta.get("indexed_vectors", 0)
        prev_n = prev_meta.get("indexed_notes", 0)
        updated_meta = dict(prev_meta)
        updated_meta.update({
            "status": "unverified",
            "unverified": True,
            "unverified_reason": "modified_by_generic_indexing",
            "indexing_source": "index_corpus",
            "indexed_vectors": vectors_stored if not prev_v else prev_v + vectors_stored,
            "indexed_notes": files_indexed if not prev_n else prev_n + files_indexed,
            "operational_corpus_snapshot": snap,
        })
        store.save_metadata(updated_meta)
    return (files_indexed, units_indexed, vectors_stored, files_skipped)


def describe_index_state(store, manifest) -> dict:
    """Describe whether ``store`` covers ``manifest`` without mutating.

    Returns the freshness report plus the stored row count. The vault
    manifest is the source of truth; the vector store is derived state.
    """
    try:
        rows = store.show_all()
    except Exception:
        rows = []
    indexed = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        if not isinstance(source, str) or not source:
            continue
        text = row.get("text", "") if isinstance(row.get("text"), str) else ""
        import hashlib
        indexed[source] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    from src.living_authenticity.knowledge.vault.context import (
        verify_index_freshness,
    )
    report = verify_index_freshness(manifest, indexed)
    report["stored_rows"] = len(rows) if isinstance(rows, list) else 0
    return report


def synchronize_index(corpus_root, pipeline, embedder, store,
                       max_files: int = 1,
                       manifest=None) -> dict:
    """Explicitly rebuild a stale index into a fresh store.

    Refuses when ``store`` is not empty (same rule as ``index_corpus``):
    the operator supplies a fresh empty vector directory and the sync
    result records the pre-sync freshness report plus the indexing
    outcome. No background watcher, no auto-mutation, no authoritative
    semantics — an explicit operator action with an inspectable report.
    """
    from src.living_authenticity.knowledge.vault.context import snapshot_vault
    before = describe_index_state(store, manifest or {})
    existing = _existing_vector_count(store)
    if existing:
        return {"action": "refused_non_empty_store",
                "stored_rows": existing, "freshness_before": before,
                "indexed": None}
    outcome = index_corpus(corpus_root, pipeline, embedder, store,
                           max_files=max_files)
    try:
        current = snapshot_vault(corpus_root, max_files=max_files)
    except Exception:
        current = {}
    after = describe_index_state(store, current)
    return {"action": "indexed", "stored_rows": outcome[2],
            "freshness_before": before, "freshness_after": after,
            "indexed": {"files_indexed": outcome[0],
                        "units_indexed": outcome[1],
                        "vectors_stored": outcome[2],
                        "files_skipped": outcome[3]}}
