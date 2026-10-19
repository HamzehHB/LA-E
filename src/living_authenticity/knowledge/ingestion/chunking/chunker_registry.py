"""Chunker registry for deterministic document chunking.

The active pipeline uses deterministic chunkers:
- MarkdownChunker for '.md' documents
- ParagraphChunker for '.txt' documents

Note: SemanticBoundaryDetector (semantic_boundary.py) is an experimental
prototype preserved for ongoing research into boundary detection. It is
opt-in and intentionally not registered by default to preserve deterministic,
reproducible chunk boundaries across all standard ingestion runs.
"""
from pathlib import Path

from .markdown_chunker import MarkdownChunker
from .paragraph_chunker import ParagraphChunker


class ChunkerRegistry:
    """
    Chooses the appropriate chunker.
    """

    def __init__(self):
        self._chunkers = {
            ".md": MarkdownChunker(),
            ".txt": ParagraphChunker(),
            "chat_input": MarkdownChunker(),
            "text": ParagraphChunker(),
        }

    def register(self, identifier: str, chunker):
        self._chunkers[identifier.lower()] = chunker

    def get(self, file_path_or_identifier: str):
        if not isinstance(file_path_or_identifier, str):
            raise TypeError("file_path_or_identifier must be a string")

        extension = Path(file_path_or_identifier).suffix.lower()
        if extension and extension in self._chunkers:
            return self._chunkers[extension]

        lowered = file_path_or_identifier.lower()
        if lowered in self._chunkers:
            return self._chunkers[lowered]

        if f".{lowered}" in self._chunkers:
            return self._chunkers[f".{lowered}"]

        err_key = extension if extension else file_path_or_identifier
        raise ValueError(f"No chunker registered for '{err_key}'")