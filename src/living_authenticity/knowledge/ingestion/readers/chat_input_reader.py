from pathlib import Path

from .base_reader import BaseReader


class ChatInputReader(BaseReader):
    """
    Reader for interactive chat and direct text input.

    Reads in-memory text directly or reads text from a file if given a path.
    Preserves raw content without modification.
    """

    def read(self, file_path: str) -> str:
        path = Path(file_path)
        with path.open("r", encoding="utf-8-sig") as file:
            return file.read()

    def read_text(self, text: str) -> str:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if text.startswith("\ufeff"):
            return text[1:]
        return text
