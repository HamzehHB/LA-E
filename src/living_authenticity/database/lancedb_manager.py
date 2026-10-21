import uuid

import lancedb

from Config.settings import PATHS
from src.living_authenticity.database.schema import KNOWLEDGE_VECTOR_SCHEMA


class DatabaseError(Exception):
    """Base exception for database failures."""


class DatabaseOperationError(DatabaseError):
    """Raised when an operation against the database (insert, delete, metadata) fails."""


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

    def store(self, text: str, embedding, metadata: dict):
        table = self.get_table()

        try:
            names = set(table.schema.names)
        except Exception:
            names = set()

        row = {
            "id": str(uuid.uuid4()),
            "text": text,
            "source": metadata["source"],
            "embedding": embedding.tolist() if hasattr(embedding, "tolist") else list(embedding),
        }
        if "position" in names and "position" in metadata:
            row["position"] = int(metadata["position"])
        try:
            table.add([row])
        except Exception as exc:
            raise DatabaseOperationError(f"Failed storing vector row: {exc}") from exc

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
        except Exception:
            return set()

    def get_metadata(self) -> dict:
        """Read index metadata from the database if present."""
        import json
        try:
            if "index_metadata" in self._table_names():
                tbl = self.db.open_table("index_metadata")
                rows = tbl.to_arrow().to_pylist()
                if rows and "payload" in rows[0]:
                    return json.loads(rows[0]["payload"])
        except Exception:
            return {}
        return {}

    def save_metadata(self, metadata: dict) -> None:
        """Persist index metadata into the database."""
        import json
        import pyarrow as pa
        payload = json.dumps(metadata, ensure_ascii=False)
        row = [{"id": "meta", "payload": payload}]
        schema = pa.schema([
            pa.field("id", pa.string()),
            pa.field("payload", pa.string()),
        ])
        try:
            if "index_metadata" in self._table_names():
                tbl = self.db.open_table("index_metadata")
                tbl.delete("id IS NOT NULL")
                tbl.add(row)
            else:
                self.db.create_table("index_metadata", data=row, schema=schema, exist_ok=True)
        except Exception as exc:
            raise DatabaseOperationError(f"Failed saving index metadata: {exc}") from exc

    def show_all(self):
        table = self.get_table()
        return table.to_arrow().to_pylist()