from .loader import load_embedding_model


class EmbeddingService:
    def __init__(self, model_path: str):
        self.model = load_embedding_model(model_path)

    @property
    def dimension(self) -> int:
        """Return embedding vector dimension."""
        if hasattr(self.model, "get_dimension"):
            return int(self.model.get_dimension())
        return int(getattr(self.model, "dimension", 0))

    def embed(self, text: str):
        return self.model.encode([text])[0]

    def embed_batch(self, texts):
        """Embed many texts in one call, preserving input order."""
        items = list(texts)
        for item in items:
            if not isinstance(item, str):
                raise TypeError("texts must hold strings")
        if not items:
            return []
        return list(self.model.encode(items))