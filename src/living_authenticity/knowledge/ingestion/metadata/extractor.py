from pathlib import Path
from typing import Any


class MetadataExtractor:
    """
    Extracts file-level metadata for an ingested source file.
    """

    def extract(
        self,
        source: str,
        reader: str = "",
        chunk_count: int = 0,
        input_id: str = "",
        input_hash: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        path = Path(source)

        meta: dict[str, Any] = {
            "source": str(source),
            "file_name": path.name,
            "extension": path.suffix,
        }
        if reader:
            meta["reader"] = str(reader)
        if chunk_count > 0:
            meta["chunk_count"] = int(chunk_count)
        if input_id:
            meta["input_id"] = str(input_id)
        if input_hash:
            meta["input_hash"] = str(input_hash)
        for k, v in extra.items():
            if v is not None and k not in meta:
                meta[k] = v
        return meta