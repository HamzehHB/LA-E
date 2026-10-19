import re
import unicodedata


class Normalizer:
    """
    Normalizes cleaned text representation.

    Normalization further refines the cleaned representation without
    altering substantive meaning:
    - Unicode NFC normalization (ensuring canonical precomposition)
    - Collapse multiple spaces within lines to one (preserving indentation)
    """

    def normalize(self, text: str) -> str:
        """
        Normalize cleaned text.

        Ensures canonical Unicode NFC precomposition and collapses
        runs of multiple spaces into a single space within each line,
        preserving line structure and leading indentation.
        """
        text = unicodedata.normalize("NFC", text)
        lines = [re.sub(r"(?<=\S) {2,}", " ", line) for line in text.split("\n")]
        return "\n".join(lines)