from pathlib import Path

from .base_reader import BaseReader
from .chat_input_reader import ChatInputReader
from .markdown_reader import MarkdownReader
from .txt_reader import TXTReader


class ReaderRegistry:
    """
    Returns the correct reader based on file extension or source type.
    """

    def __init__(self):
        self._readers: dict[str, BaseReader] = {
            ".txt": TXTReader(),
            ".md": MarkdownReader(),
            "chat_input": ChatInputReader(),
            "text": TXTReader(),
        }

    def register(self, identifier: str, reader: BaseReader):
        self._readers[identifier.lower()] = reader

    def get(self, file_path_or_identifier: str) -> BaseReader:
        if not isinstance(file_path_or_identifier, str):
            raise TypeError("file_path_or_identifier must be a string")

        extension = Path(file_path_or_identifier).suffix.lower()
        if extension and extension in self._readers:
            return self._readers[extension]

        lowered = file_path_or_identifier.lower()
        if lowered in self._readers:
            return self._readers[lowered]

        if f".{lowered}" in self._readers:
            return self._readers[f".{lowered}"]

        err_key = extension if extension else file_path_or_identifier
        raise ValueError(f"No reader registered for '{err_key}'")

    @property
    def supported_formats(self) -> tuple[str, ...]:
        return tuple(sorted(self._readers.keys()))