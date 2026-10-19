import unicodedata


class Cleaner:
    """
    Cleans raw text before normalization, parsing, chunking, and extraction.

    Cleaning normalizes representation without altering substantive meaning:
    - Unicode NFC normalization
    - Line ending normalization (CRLF/CR -> LF)
    - Trailing whitespace removal per line
    - Collapse of excessive blank lines (3+ -> 2)
    """

    def clean(self, text: str) -> str:
        """
        Clean raw text.

        Normalizes Unicode, line endings, and whitespace while
        preserving all meaningful content and structure.
        """
        # Unicode NFC normalization
        text = unicodedata.normalize("NFC", text)

        # Normalize line endings to LF
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # Remove trailing whitespace per line
        lines = [line.rstrip() for line in text.split("\n")]
        text = "\n".join(lines)

        # Collapse 3+ consecutive blank lines to 2
        while "\n\n\n" in text:
            text = text.replace("\n\n\n", "\n\n")

        return text