import json
import os
import sys
from pathlib import Path
from typing import Any
import uuid

import lancedb
import pyarrow as pa

from Config.settings import PATHS
from src.living_authenticity.database.schema import KNOWLEDGE_VECTOR_SCHEMA


class DatabaseError(Exception):
    """Base exception for database failures."""


class DatabaseOperationError(DatabaseError):
    """Raised when an operation against the database (insert, delete, metadata) fails."""


class DatabaseLockError(DatabaseOperationError):
    """Raised when an operation cannot acquire a database lock (retained for backward compatibility)."""


class VaultMetadataError(DatabaseOperationError):
    """Base exception for index metadata operations and validation."""


class VaultMetadataMissingError(VaultMetadataError):
    """Raised when a vector store has existing records but metadata is absent."""


class VaultMetadataCorruptionError(VaultMetadataError):
    """Raised when index metadata cannot be deserialized or is corrupted."""


class LanceDBManager:
    def __init__(self, db_path=None):
        target = db_path if db_path is not None else PATHS["vector_db"]["lancedb"]
        self.db_path = str(target)
        self.db = lancedb.connect(target)

    def create_knowledge_vector_table(self):
        return self.db.create_table(
            "knowledge_vectors",
            schema=KNOWLEDGE_VECTOR_SCHEMA,
            exist_ok=True,
        )

    def get_table(self):
        return self.db.open_table("knowledge_vectors")

    def store(
        self,
        text: str,
        embedding,
        metadata: dict,
        record_id: str | None = None,
        validated_core: Any = None,
    ):
        table = self.get_table()

        try:
            names = set(table.schema.names)
        except Exception as exc:
            raise DatabaseOperationError(f"Failed inspecting table schema: {exc}") from exc

        from src.living_authenticity.knowledge.vault.core_validator import (
            is_trusted_validation_context,
            compute_core_record_id,
            normalize_source_identity,
        )

        val_ctx = getattr(validated_core, "validation_context", None)
        is_trusted_call = (
            validated_core is not None
            and is_trusted_validation_context(val_ctx)
        )

        raw_is_core = bool(metadata.get("is_core", False))
        eff_is_core = False
        eff_core_hash = ""
        eff_record_id = None

        if is_trusted_call and raw_is_core:
            # Independent verification: ensure this record corresponds to an actual validated Core unit!
            req_source = normalize_source_identity(str(metadata.get("source", "")))
            expected_source = normalize_source_identity(str(getattr(val_ctx, "source_path", "")))
            req_hash = str(metadata.get("core_hash") or "")
            expected_hash = str(getattr(val_ctx, "core_hash", ""))
            req_pos = int(metadata.get("position", 0)) if "position" in metadata else 0

            matching_unit = None
            for u in getattr(validated_core, "units", ()):
                u_source = normalize_source_identity(str(getattr(u, "core_source_path", "") or getattr(u, "source", "")))
                u_pos = int(getattr(u, "position", 0))
                unit_texts = {
                    t.strip() for t in (
                        getattr(u, "cleaned_text", None),
                        getattr(u, "original_text", None),
                        getattr(u, "normalized_text", None),
                        getattr(u, "meaning", None),
                    ) if t and isinstance(t, str) and t.strip()
                }
                clean_in_text = text.strip()
                if (
                    u_source == req_source
                    and u_pos == req_pos
                    and clean_in_text in unit_texts
                ):
                    matching_unit = u
                    break

            if (
                matching_unit is not None
                and req_source == expected_source
                and req_hash == expected_hash
            ):
                expected_rec_id = compute_core_record_id(matching_unit, validated_core)
                if record_id is None or record_id == expected_rec_id:
                    eff_is_core = True
                    eff_core_hash = expected_hash
                    eff_record_id = expected_rec_id
                    # Enforce database uniqueness for Core records (replace any duplicate)
                    try:
                        table.delete(f"id = '{eff_record_id}'")
                    except Exception as exc:
                        raise DatabaseOperationError(
                            f"Failed deleting prior vector row with id '{eff_record_id}': {exc}"
                        ) from exc

        if not eff_is_core:
            eff_is_core = False
            eff_core_hash = ""
            # Untrusted callers or mismatched records CANNOT set is_core=True or specify Core record IDs
            if record_id and str(record_id).startswith("core_vec_"):
                eff_record_id = str(uuid.uuid4())
            else:
                eff_record_id = str(record_id) if record_id else (
                    str(metadata.get("record_id")) if metadata.get("record_id") and not str(metadata.get("record_id")).startswith("core_vec_") else (
                        str(metadata.get("id")) if metadata.get("id") and not str(metadata.get("id")).startswith("core_vec_") else str(uuid.uuid4())
                    )
                )

        row = {
            "id": eff_record_id,
            "text": text,
            "source": metadata["source"],
            "embedding": embedding.tolist() if hasattr(embedding, "tolist") else list(embedding),
        }
        if "position" in names and "position" in metadata:
            row["position"] = int(metadata["position"])
        if "is_core" in names:
            row["is_core"] = eff_is_core
        if "core_hash" in names:
            row["core_hash"] = eff_core_hash
        try:
            table.add([row])
        except Exception as exc:
            raise DatabaseOperationError(f"Failed storing vector row: {exc}") from exc
        return eff_record_id

    def delete_source(self, source: str) -> int:
        """Delete all vector rows associated with one source note."""
        if not isinstance(source, str) or not source.strip():
            return 0
        table = self.get_table()
        escaped = source.replace("'", "''")
        try:
            table.delete(f"source = '{escaped}'")
            return 1
        except Exception as exc:
            raise DatabaseOperationError(f"Failed deleting vectors for source '{source}': {exc}") from exc

    def delete_all(self) -> None:
        """Clear all vector rows from the knowledge vectors table."""
        try:
            table = self.get_table()
            table.delete("id IS NOT NULL")
        except Exception as exc:
            raise DatabaseOperationError(f"Failed clearing knowledge vector table: {exc}") from exc

    def _table_names(self) -> set[str]:
        """Return the set of existing table names."""
        if not hasattr(self, "db") or self.db is None:
            return set()
        try:
            res = self.db.list_tables()
            if hasattr(res, "tables"):
                return set(res.tables)
            if isinstance(res, (list, tuple, set)):
                return set(res)
        except Exception:
            pass
        try:
            return set(self.db.table_names())
        except Exception as exc:
            raise DatabaseOperationError(
                f"Failed listing database tables: {exc}"
            ) from exc

    def _validate_lifecycle_marker(self) -> bool:
        """Validate that the _store_lifecycle marker exists and contains a valid record.

        Returns:
            True if marker exists and is valid.
            False if marker table is absent (store uninitialized).
        Raises:
            DatabaseOperationError if marker table exists but is corrupted, empty, or unreadable.
        """
        try:
            tables = self._table_names()
        except Exception as exc:
            if isinstance(exc, DatabaseOperationError):
                raise
            raise DatabaseOperationError(f"Failed listing tables for lifecycle marker check: {exc}") from exc

        if "_store_lifecycle" not in tables:
            return False

        try:
            tbl = self.db.open_table("_store_lifecycle")
            rows = tbl.to_arrow().to_pylist()
        except Exception as exc:
            if isinstance(exc, DatabaseOperationError):
                raise
            raise DatabaseOperationError(
                f"Operational failure opening or reading _store_lifecycle table: {exc}"
            ) from exc

        if not rows or not isinstance(rows[0], dict):
            raise DatabaseOperationError(
                "Invalid _store_lifecycle marker: table exists but contains no valid records"
            )
        if rows[0].get("id") != "init" or not rows[0].get("initialized_at"):
            raise DatabaseOperationError(
                "Invalid _store_lifecycle marker: missing required 'init' attributes"
            )
        return True

    def _mark_initialized(self) -> None:
        """Record and validate deterministic persistent initialization marker."""
        try:
            tables = self._table_names()
        except Exception as exc:
            if isinstance(exc, DatabaseOperationError):
                raise
            raise DatabaseOperationError(f"Failed listing tables for store lifecycle marker: {exc}") from exc

        if "_store_lifecycle" not in tables:
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("initialized_at", pa.string()),
            ])
            import datetime
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            row = [{"id": "init", "initialized_at": now_iso}]
            try:
                self.db.create_table("_store_lifecycle", data=row, schema=schema, exist_ok=True)
            except Exception as exc:
                if isinstance(exc, DatabaseOperationError):
                    raise
                raise DatabaseOperationError(
                    f"Failed creating store lifecycle initialization marker: {exc}"
                ) from exc

        # Validate that the marker table actually contains the valid initialization record
        self._validate_lifecycle_marker()

    def get_metadata(self) -> dict:
        """Read index metadata from the database if present.

        Fails closed with typed exceptions:
        - DatabaseOperationError: for operational database access, I/O, or table read failures.
        - VaultMetadataMissingError: if metadata table or required payload is missing in an initialized store.
        - VaultMetadataCorruptionError: if metadata payload is corrupted, malformed JSON, or structurally invalid.
        """
        tables = self._table_names()
        has_vectors = "knowledge_vectors" in tables
        has_metadata = "index_metadata" in tables
        has_lifecycle = self._validate_lifecycle_marker()

        vector_count = 0
        if has_vectors:
            try:
                tbl = self.get_table()
                vector_count = tbl.count_rows() if hasattr(tbl, "count_rows") else len(tbl.to_arrow())
            except Exception as exc:
                if isinstance(exc, DatabaseOperationError):
                    raise
                raise DatabaseOperationError(
                    f"Failed inspecting vector count in knowledge_vectors table: {exc}"
                ) from exc

        if not has_metadata:
            if has_lifecycle:
                raise VaultMetadataMissingError(
                    "Vector store is initialized but index_metadata table is missing"
                )
            return {}

        # 1. Open and read index_metadata table (Operational vs Corruption classification)
        try:
            tbl = self.db.open_table("index_metadata")
            rows = tbl.to_arrow().to_pylist()
        except Exception as exc:
            if isinstance(exc, (VaultMetadataError, DatabaseOperationError)):
                raise
            # Typed PyArrow storage corruption
            if isinstance(exc, (pa.ArrowInvalid, pa.ArrowSerializationError, pa.ArrowTypeError)):
                raise VaultMetadataCorruptionError(f"Corrupted storage format in index_metadata table: {exc}") from exc
            # Known operational I/O, filesystem, permission, and timeout failures
            if isinstance(exc, (OSError, TimeoutError)):
                raise DatabaseOperationError(f"Operational failure opening or reading index_metadata table: {exc}") from exc
            # Table not found (e.g. concurrent drop between table_names and open_table)
            if isinstance(exc, ValueError) and "not found" in str(exc).lower():
                if has_lifecycle:
                    raise VaultMetadataMissingError("Vector store is initialized but index_metadata table is missing") from exc
                return {}
            # LanceDB Rust-level Corrupt error wrapped in RuntimeError
            if isinstance(exc, RuntimeError) and "lanceerror(corrupt)" in str(exc).lower():
                raise VaultMetadataCorruptionError(f"Low-level Lance storage corruption in index_metadata: {exc}") from exc
            # All other database-access/runtime exceptions default to operational failures fail-closed
            raise DatabaseOperationError(f"Operational failure opening or reading index_metadata table: {exc}") from exc

        if not rows:
            if has_vectors and vector_count > 0:
                raise VaultMetadataMissingError("index_metadata table exists but contains no valid payload rows")
            return {}

        first_row = rows[0]
        if not isinstance(first_row, dict) or "payload" not in first_row:
            raise VaultMetadataCorruptionError("index_metadata table exists but lacks required 'payload' field")

        raw_payload = first_row["payload"]
        if not isinstance(raw_payload, str):
            raise VaultMetadataCorruptionError("index_metadata payload field is not a string")

        try:
            parsed = json.loads(raw_payload)
        except Exception as exc:
            raise VaultMetadataCorruptionError(f"Corrupted JSON in index_metadata payload: {exc}") from exc

        if not isinstance(parsed, dict):
            raise VaultMetadataCorruptionError("index_metadata payload is not a valid JSON dictionary")

        if not parsed and has_vectors and vector_count > 0:
            raise VaultMetadataMissingError("index_metadata payload is empty dictionary in populated vector store")

        return parsed

    def inspect_store_state(self) -> dict:
        """Inspect the store state and categorize into canonical state."""
        try:
            tables = self._table_names()
        except Exception as exc:
            return {
                "has_vectors_table": False,
                "has_metadata_table": False,
                "has_lifecycle_marker": False,
                "vector_count": 0,
                "metadata": None,
                "state": "error",
                "error": str(exc),
            }

        has_vectors = "knowledge_vectors" in tables
        has_metadata = "index_metadata" in tables
        vector_count = 0
        if has_vectors:
            try:
                tbl = self.get_table()
                vector_count = tbl.count_rows() if hasattr(tbl, "count_rows") else len(tbl.to_arrow())
            except Exception as exc:
                return {
                    "has_vectors_table": has_vectors,
                    "has_metadata_table": has_metadata,
                    "has_lifecycle_marker": "_store_lifecycle" in tables,
                    "vector_count": 0,
                    "metadata": None,
                    "state": "error",
                    "error": str(exc),
                }

        try:
            has_lifecycle = self._validate_lifecycle_marker()
        except Exception as exc:
            return {
                "has_vectors_table": has_vectors,
                "has_metadata_table": has_metadata,
                "has_lifecycle_marker": True,
                "vector_count": vector_count,
                "metadata": None,
                "state": "error",
                "error": f"Failed validating store lifecycle marker: {exc}",
            }

        metadata = None
        if not has_metadata:
            if has_lifecycle:
                # An initialized store where metadata table is missing must never produce genuine_first_init
                if not has_vectors or vector_count == 0:
                    state = "inconsistent_initialized"
                else:
                    state = "missing_metadata"
            else:
                # Genuinely uninitialized store
                if not has_vectors or vector_count == 0:
                    state = "genuine_first_init"
                else:
                    state = "missing_metadata"
        else:
            try:
                metadata = self.get_metadata()
                if not metadata:
                    if vector_count > 0:
                        state = "missing_metadata"
                    else:
                        state = "uninitialized_empty"
                elif not has_lifecycle:
                    # Metadata table exists without valid lifecycle marker: incomplete or inconsistent initialization
                    state = "inconsistent_initialized"
                else:
                    meta_status = str(metadata.get("status", "")).lower()
                    if meta_status in ("sync_in_progress", "failed"):
                        state = "interrupted_sync"
                    elif meta_status == "unverified" or metadata.get("unverified") is True:
                        state = "unverified"
                    elif meta_status == "fresh":
                        state = "valid"
                    else:
                        state = "unverified"
            except VaultMetadataMissingError:
                state = "missing_metadata"
            except VaultMetadataCorruptionError:
                state = "corrupted_metadata"
            except Exception as exc:
                return {
                    "has_vectors_table": has_vectors,
                    "has_metadata_table": has_metadata,
                    "has_lifecycle_marker": has_lifecycle,
                    "vector_count": vector_count,
                    "metadata": None,
                    "state": "error",
                    "error": str(exc),
                }

        return {
            "has_vectors_table": has_vectors,
            "has_metadata_table": has_metadata,
            "has_lifecycle_marker": has_lifecycle,
            "vector_count": vector_count,
            "metadata": metadata,
            "state": state,
        }

    def get_existing_core_record_ids(self, source_path: str) -> set[str]:
        """Return the set of existing vector record IDs for an authoritative Core note."""
        if not isinstance(source_path, str) or not source_path.strip():
            return set()
        try:
            table = self.get_table()
            rows = table.to_arrow().to_pylist()
            escaped = source_path.replace("\\", "/").lower()
            return {
                str(r["id"]) for r in rows
                if isinstance(r, dict)
                and r.get("id")
                and bool(r.get("is_core", False))
                and str(r.get("source", "")).replace("\\", "/").lower() == escaped
            }
        except Exception as exc:
            raise DatabaseOperationError(
                f"Failed querying existing Core record IDs for '{source_path}': {exc}"
            ) from exc

    def save_metadata(
        self,
        metadata: dict,
        validated_core: Any = None,
        sync_auth: Any = None,
        expected_session_id: str | None = None,
        expected_ticket_id: str | None = None,
        expected_vault_id: str | None = None,
    ) -> None:
        """Persist index metadata into the database using a guarded, fail-closed sequence.

        Note on persistence guarantees:
        LanceDB does not provide multi-table ACID transactions across separate tables.
        Metadata persistence and lifecycle-marker registration are executed as a guarded,
        fail-closed sequence with compensating rollback:
        - On first initialization, if lifecycle-marker creation or validation fails after
          index_metadata is written, compensating rollback immediately attempts to remove
          the newly created index_metadata table to prevent leaving an orphaned, unmarked
          state. If rollback cannot be completed or verified, the store fails closed and
          refuses genuine-first-initialization privileges.
        - On updates to already-initialized stores, existing metadata is preserved without
          destructive rollback.

        Fail-closed baseline certification rules:
        - Baseline fields ('core_vector_digest', 'core_vector_count', 'core_record_ids',
          'core_hash', 'core_source_path', 'vector_table_version') can ONLY be established
          or replaced when BOTH an authoritative, trusted validated_core AND a valid,
          matching sync_auth capability from the authorized synchronization lifecycle
          are provided.
        - ValidatedCore proof establishes Core identity and validation provenance;
          sync_auth establishes human-authorized synchronization privilege. A valid
          Core does NOT automatically authorize baseline certification.
        - If validated_core is provided without valid sync_auth:
          - Fail closed immediately with DatabaseOperationError.
        - If sync_auth is provided without validated_core:
          - Fail closed immediately with DatabaseOperationError.
        - If sync_auth is forged, expired, untrusted, or has mismatched core_hash:
          - Fail closed immediately with DatabaseOperationError.
        - If neither is provided (generic metadata update):
          - Baseline fields CANNOT be established or modified.
          - If existing index_metadata already contains an established baseline, those existing
            baseline fields are strictly PRESERVED, while non-baseline metadata updates
            (e.g., status, manifest, last_synced_at, indexed_notes, indexed_vectors) are applied.
          - If existing index_metadata does NOT contain a baseline, baseline keys are stripped
            from eff_meta so unverified baselines cannot be created out of thin air.
        """
        eff_meta = dict(metadata)
        from src.living_authenticity.knowledge.vault.core_validator import (
            CoreValidationContext,
            SyncAuthorizationContext,
            compute_core_vector_digest,
            claim_and_consume_sync_authorization,
            is_trusted_sync_authorization,
            is_trusted_validation_context,
        )

        has_val_core = validated_core is not None
        has_sync_auth = sync_auth is not None

        # Fail closed on mismatched, untrusted, or forged parameters
        if has_val_core and not has_sync_auth:
            raise DatabaseOperationError(
                "Core baseline certification requires valid, authorized synchronization evidence"
            )
        if not has_val_core and has_sync_auth:
            raise DatabaseOperationError(
                "Cannot certify Core baseline without validated Core"
            )

        tables = self._table_names()
        has_metadata = "index_metadata" in tables
        has_lifecycle = self._validate_lifecycle_marker()

        if has_metadata and not has_lifecycle:
            raise DatabaseOperationError(
                "Vector store is in an inconsistent state: index_metadata table is present "
                "but store lifecycle marker is absent. Metadata persistence denied. "
                "Store reset or clean rebuild required."
            )

        baseline_keys = (
            "core_vector_digest",
            "core_vector_count",
            "core_record_ids",
            "core_hash",
            "core_source_path",
            "vector_table_version",
        )

        if has_val_core and has_sync_auth:
            val_ctx = getattr(validated_core, "validation_context", None)
            if not is_trusted_validation_context(val_ctx) or not isinstance(val_ctx, CoreValidationContext):
                raise DatabaseOperationError(
                    "Untrusted or invalid validation context provided for Core baseline certification"
                )

            eff_vault_id = expected_vault_id or eff_meta.get("vault_id")
            claimed = claim_and_consume_sync_authorization(
                sync_auth,
                expected_core_hash=val_ctx.core_hash,
                expected_vault_id=eff_vault_id,
                expected_session_id=expected_session_id,
                expected_ticket_id=expected_ticket_id,
            )
            if not claimed or not isinstance(sync_auth, SyncAuthorizationContext):
                raise DatabaseOperationError(
                    "Untrusted or invalid synchronization authorization provided for Core baseline certification"
                )

            cand_core_hash = eff_meta.get("core_hash")
            if cand_core_hash and cand_core_hash != val_ctx.core_hash:
                raise DatabaseOperationError(
                    f"Core hash in metadata candidate ('{cand_core_hash}') does not match "
                    f"trusted validation context ('{val_ctx.core_hash}')"
                )
            eff_meta["core_hash"] = val_ctx.core_hash
            if "core_source_path" not in eff_meta:
                eff_meta["core_source_path"] = val_ctx.source_path

            if "knowledge_vectors" in self._table_names():
                try:
                    tbl = self.get_table()
                    if "vector_table_version" not in eff_meta:
                        raw_v = getattr(tbl, "version", None)
                        if raw_v is not None and str(raw_v).strip():
                            eff_meta["vector_table_version"] = str(raw_v).strip()
                    if (
                        "core_vector_digest" not in eff_meta
                        or "core_vector_count" not in eff_meta
                        or "core_record_ids" not in eff_meta
                    ):
                        rows = self.show_all()
                        core_rows = [
                            r for r in rows
                            if isinstance(r, dict)
                            and bool(r.get("is_core", False))
                        ]
                        if "core_vector_digest" not in eff_meta:
                            eff_meta["core_vector_digest"] = (
                                compute_core_vector_digest(core_rows) if core_rows else ""
                            )
                        if "core_vector_count" not in eff_meta:
                            eff_meta["core_vector_count"] = len(core_rows)
                        if "core_record_ids" not in eff_meta:
                            eff_meta["core_record_ids"] = sorted(
                                str(r.get("id", "")) for r in core_rows
                            )
                except Exception as exc:
                    raise DatabaseOperationError(
                        f"Failed deriving Core baseline integrity during authorized save_metadata: {exc}"
                    ) from exc
        else:
            # Generic update or unverified call: cannot establish or mutate baseline!
            # Distinguish a genuinely empty/new store from corrupted, inaccessible, or inconsistent metadata.
            # Fail closed: inspect existing metadata without swallowing read, corruption, or missing metadata errors!
            tables = self._table_names()
            has_metadata = "index_metadata" in tables
            has_lifecycle = self._validate_lifecycle_marker()

            if has_metadata:
                existing_meta: dict[str, Any] = self.get_metadata()
            elif has_lifecycle:
                # Store was initialized previously, but index_metadata table has been lost/dropped!
                raise VaultMetadataMissingError(
                    "Vector store is initialized but index_metadata table is missing"
                )
            else:
                # Legitimate first initialization (Behavior A):
                # Genuinely new store (either empty or with initial vectors inserted before first save_metadata)
                existing_meta = {}

            has_existing_baseline = bool(
                existing_meta.get("core_vector_digest")
                and existing_meta.get("core_hash")
            )

            if has_existing_baseline:
                # Strictly preserve the existing trusted baseline fields
                for k in (
                    "core_vector_digest",
                    "core_vector_count",
                    "core_record_ids",
                    "core_hash",
                    "core_source_path",
                    "vector_table_version",
                ):
                    if k in existing_meta:
                        eff_meta[k] = existing_meta[k]
                    else:
                        eff_meta.pop(k, None)
            else:
                # No trusted baseline exists yet: refuse to create vector baseline out of thin air
                for k in ("core_vector_digest", "core_vector_count", "core_record_ids"):
                    eff_meta.pop(k, None)
                if "core_hash" in existing_meta:
                    eff_meta["core_hash"] = existing_meta["core_hash"]
                if "core_source_path" in existing_meta:
                    eff_meta["core_source_path"] = existing_meta["core_source_path"]
                if "vector_table_version" in existing_meta:
                    eff_meta["vector_table_version"] = existing_meta["vector_table_version"]

        had_metadata_before = "index_metadata" in self._table_names()
        payload = json.dumps(eff_meta, ensure_ascii=False)
        row = [{"id": "meta", "payload": payload}]
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("payload", pa.string()),
        ])
        try:
            if had_metadata_before:
                tbl = self.db.open_table("index_metadata")
                tbl.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(row)
            else:
                self.db.create_table("index_metadata", data=row, schema=schema, exist_ok=True)
            self._mark_initialized()
        except Exception as exc:
            if not had_metadata_before:
                # First initialization did not complete: attempt fail-closed rollback of newly created index_metadata
                rollback_exc: Exception | None = None
                try:
                    self.drop_table("index_metadata")
                except Exception as r_exc:
                    rollback_exc = r_exc

                # Verify actual database state after rollback
                tables_post = set()
                try:
                    tables_post = self._table_names()
                except Exception:
                    tables_post = {"index_metadata"}  # fail-closed assumption

                if "index_metadata" in tables_post or rollback_exc is not None:
                    rb_detail = f"; rollback failed: {rollback_exc}" if rollback_exc else "; index_metadata table remains present"
                    raise DatabaseOperationError(
                        f"Initial metadata save failed during lifecycle registration ({exc}){rb_detail}"
                    ) from exc

            if isinstance(exc, DatabaseOperationError):
                raise
            raise DatabaseOperationError(f"Failed saving index metadata: {exc}") from exc

    def drop_table(self, table_name: str) -> None:
        """Drop a table if it exists."""
        try:
            if table_name in self._table_names():
                self.db.drop_table(table_name)
        except Exception as exc:
            raise DatabaseOperationError(f"Failed dropping table '{table_name}': {exc}") from exc

    def show_all(self):
        table = self.get_table()
        return table.to_arrow().to_pylist()
