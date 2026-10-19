import re

from .base_extractor import KnowledgeUnitExtractor, make_unit_id
from .knowledge_unit import KnowledgeUnit

_MIN_SENTENCE_WORDS = 3


def _is_independent_sentence(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[0].isupper():
        return True
    if stripped[0].isdigit():
        return True
    if len(stripped.split()) >= _MIN_SENTENCE_WORDS:
        return True
    return False


class PlainTextKnowledgeExtractor(KnowledgeUnitExtractor):
    """Extractor for plain text without Obsidian labels.

    Splits on blank-line blocks. A block of independent list items becomes
    one unit per item; otherwise a block stays as one unit so related
    sentences remain together.
    """

    def extract(
        self,
        source: str,
        original_text: str,
        cleaned_text: str,
        normalized_text: str,
        parsed=None,
        parser=None,
        metadata: dict | None = None,
        input_provenance: object | None = None,
        **kwargs,
    ) -> list[KnowledgeUnit]:
        if not cleaned_text.strip():
            return []

        input_id = getattr(input_provenance, "input_id", "") or (
            metadata.get("input_id", "") if isinstance(metadata, dict) else ""
        )
        input_hash = getattr(input_provenance, "input_hash", "") or (
            metadata.get("input_hash", "") if isinstance(metadata, dict) else ""
        )

        blocks = [b for b in re.split(r"\n\s*\n", cleaned_text) if b.strip()]
        units: list[KnowledgeUnit] = []

        for index, block in enumerate(blocks):
            items = [
                ln.strip().lstrip("-*• \t").strip()
                for ln in block.splitlines()
                if ln.strip()
            ]
            if len(items) > 1 and all(_is_independent_sentence(it) for it in items):
                for sub_index, item in enumerate(items):
                    units.append(self._make_unit(
                        source, item, index + sub_index,
                        "independent_list_items",
                        input_id=input_id, input_hash=input_hash,
                    ))
                continue

            units.append(self._make_unit(
                source, block.strip(), index, "paragraph_fallback",
                input_id=input_id, input_hash=input_hash,
            ))

        return units

    @staticmethod
    def _make_unit(
        source: str, body: str, position: int, basis: str,
        input_id: str = "", input_hash: str = "",
    ) -> KnowledgeUnit:
        return KnowledgeUnit(
            id=make_unit_id(source, position, body),
            source=source,
            original_text=body,
            cleaned_text=body,
            normalized_text=body,
            meaning=body,
            context="",
            proposed_type="",
            position=position,
            boundary_basis=basis,
            input_id=input_id,
            input_hash=input_hash,
        )
