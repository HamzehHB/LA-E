from pathlib import Path

from .markdown_extractor import MarkdownKnowledgeExtractor
from .plain_text_extractor import PlainTextKnowledgeExtractor


class ExtractorRegistry:
    """Choose the appropriate knowledge-unit extractor by file extension."""

    def __init__(self):
        self._extractors = {
            ".md": MarkdownKnowledgeExtractor(),
            ".txt": PlainTextKnowledgeExtractor(),
            "chat_input": MarkdownKnowledgeExtractor(),
            "text": PlainTextKnowledgeExtractor(),
        }

    def register(self, identifier: str, extractor) -> None:
        self._extractors[identifier.lower()] = extractor

    def get(self, file_path_or_identifier: str):
        if not isinstance(file_path_or_identifier, str):
            raise TypeError("file_path_or_identifier must be a string")

        extension = Path(file_path_or_identifier).suffix.lower()
        if extension and extension in self._extractors:
            return self._extractors[extension]

        lowered = file_path_or_identifier.lower()
        if lowered in self._extractors:
            return self._extractors[lowered]

        if f".{lowered}" in self._extractors:
            return self._extractors[f".{lowered}"]

        err_key = extension if extension else file_path_or_identifier
        raise ValueError(
            f"No knowledge unit extractor registered for '{err_key}'"
        )
