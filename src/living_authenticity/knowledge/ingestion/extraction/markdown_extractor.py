import re

from .base_extractor import KnowledgeUnitExtractor, make_unit_id
from .knowledge_unit import KnowledgeUnit

_METADATA_LABELS = {
    "relations", "ارتباطات", "tags", "تگ", "برچسب", "origin",
    "questions", "سؤال های باز", "سوال های باز", "status", "وضعیت",
    "conflict", "تعارض", "تعارض با مدل فعلی",
}
_MIN_WORDS = 3


def _norm(label: str) -> str:
    return re.sub(r"\s+", " ", label.replace("​", " ").strip().lower())


def _independent(text: str) -> bool:
    s = text.strip()
    if not s:
        return False
    return s[0].isupper() or s[0].isdigit() or len(s.split()) >= _MIN_WORDS


def _blocks(text: str) -> list[str]:
    return [b for b in re.split(r"\n\s*\n", text) if b.strip()]


def _orig_span(span_list, idx, length):
    """Return the (start, end) for span_list[idx], clamped to length."""
    if not span_list or idx >= len(span_list):
        return 0, length
    _, start, end = span_list[idx]
    return start, end


def _strip_label_line(body: str, label: str) -> str:
    """Remove the leading label line (e.g. "Observation:") from a section body.

    ``section_boundaries`` spans include the label line, but the label is
    already captured in the unit's ``context``. It must not be treated as
    content or as a list item.
    """
    if not label:
        return body
    lines = body.splitlines()
    if lines and lines[0].strip().rstrip(":").strip() == label.strip().rstrip(":").strip():
        return "\n".join(lines[1:]).strip()
    return body



class MarkdownKnowledgeExtractor(KnowledgeUnitExtractor):
    """Semantic extractor for labeled Markdown/Obsidian text.

    Sections are structural evidence, not 1:1 units. Within a section,
    dependent sentences stay together; only clearly independent ideas split.
    """

    def extract(
        self,
        source,
        original_text,
        cleaned_text,
        normalized_text,
        parsed=None,
        parser=None,
        metadata=None,
        input_provenance=None,
        **kwargs,
    ):
        if not cleaned_text.strip():
            return []
        input_id = getattr(input_provenance, "input_id", "") or (
            metadata.get("input_id", "") if isinstance(metadata, dict) else ""
        )
        input_hash = getattr(input_provenance, "input_hash", "") or (
            metadata.get("input_hash", "") if isinstance(metadata, dict) else ""
        )
        if parsed is not None and parser is not None:
            units = self._from_parsed(
                source, original_text, cleaned_text, normalized_text, parser,
                input_id=input_id, input_hash=input_hash,
            )
            if units:
                return units
        return self._from_blocks(
            source, cleaned_text, normalized_text,
            input_id=input_id, input_hash=input_hash,
        )

    def _from_parsed(self, source, original_text, cleaned_text, normalized_text, parser,
                     input_id="", input_hash=""):
        if not hasattr(parser, "section_boundaries"):
            return None
        cb = parser.section_boundaries(cleaned_text)
        if not any(l for l, _, _ in cb):
            return None
        ob = parser.section_boundaries(original_text) if original_text else []
        units, pos = [], 0
        for i, (label, cs, ce) in enumerate(cb):
            key = _norm(label) if label else ""
            if key in _METADATA_LABELS:
                continue
            body = cleaned_text[cs:ce].strip()
            if not body:
                continue
            oe = _orig_span(ob, i, len(original_text))
            orig = original_text[oe[0]:oe[1]].strip() if original_text else ""
            for u in self._split(
                source, orig, normalized_text, cs, ce, body, label, pos,
                input_id=input_id, input_hash=input_hash,
            ):
                units.append(u)
                pos += 1
        return units or None

    def _split(self, source, orig, norm_text, ns, ne, body, label, pos,
               input_id="", input_hash=""):
        body = _strip_label_line(body, label)
        blks = _blocks(body)
        if len(blks) > 1:
            return [self._unit(
                source, orig, norm_text, ns, ne, b.strip(), label, pos + k, "section_sub_idea",
                input_id=input_id, input_hash=input_hash,
            ) for k, b in enumerate(blks)]
        items = [ln.strip().lstrip("-*• \t").strip() for ln in body.splitlines() if ln.strip()]
        if len(items) > 1 and all(_independent(it) for it in items):
            return [self._unit(
                source, orig, norm_text, ns, ne, it, label, pos + k, "independent_list_items",
                input_id=input_id, input_hash=input_hash,
            ) for k, it in enumerate(items)]
        return [self._unit(
            source, orig, norm_text, ns, ne, body, label, pos, "labeled_section",
            input_id=input_id, input_hash=input_hash,
        )]

    def _from_blocks(self, source, cleaned_text, normalized_text,
                     input_id="", input_hash=""):
        units = []
        for i, block in enumerate(_blocks(cleaned_text)):
            items = [ln.strip().lstrip("-*• \t").strip() for ln in block.splitlines() if ln.strip()]
            if len(items) > 1 and all(_independent(it) for it in items):
                for k, it in enumerate(items):
                    units.append(self._unit(
                        source, "", normalized_text, 0, 0, it, "", i + k, "independent_list_items",
                        input_id=input_id, input_hash=input_hash,
                    ))
                continue
            units.append(self._unit(
                source, "", normalized_text, 0, 0, block.strip(), "", i, "paragraph_fallback",
                input_id=input_id, input_hash=input_hash,
            ))
        return units

    @staticmethod
    def _unit(source, orig, norm_text, ns, ne, body, label, pos, basis,
              input_id="", input_hash=""):
        norm = norm_text[ns:ne].strip() if norm_text and ne else body
        return KnowledgeUnit(
            id=make_unit_id(source, pos, body), source=source,
            original_text=orig if orig else body, cleaned_text=body,
            normalized_text=norm, meaning=body, context=label or "",
            proposed_type="", position=pos, boundary_basis=basis,
            input_id=input_id, input_hash=input_hash)
