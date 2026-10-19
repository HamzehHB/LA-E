from abc import ABC, abstractmethod


class BaseReader(ABC):
    """
    Base class for all knowledge readers.
    """

    @abstractmethod
    def read(self, file_path: str) -> str:
        """
        Read a file and return its raw text.
        """
        pass

    def read_text(self, text: str) -> str:
        """
        Read direct in-memory text without filesystem access.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if text.startswith("\ufeff"):
            return text[1:]
        return text