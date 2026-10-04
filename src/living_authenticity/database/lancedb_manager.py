import uuid

import lancedb

from Config.settings import PATHS
from src.living_authenticity.database.schema import KNOWLEDGE_VECTOR_SCHEMA


class LanceDBManager:
    def __init__(self, db_path=None):
        target = db_path if db_path is not None else PATHS["vector_db"]["lancedb"]
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
            "embedding": embedding.tolist(),
        }
        if "position" in names and "position" in metadata:
            row["position"] = int(metadata["position"])
        table.add([row])

    def show_all(self):
        table = self.get_table()
        return table.to_arrow().to_pylist()