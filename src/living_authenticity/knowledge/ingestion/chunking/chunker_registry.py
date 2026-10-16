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
        }

    def register(self, extension: str, chunker):

        self._chunkers[extension.lower()] = chunker

    def get(self, file_path: str):

        extension = Path(file_path).suffix.lower()

        if extension not in self._chunkers:

            raise ValueError(
                f"No chunker registered for '{extension}'"
            )

        return self._chunkers[extension]