"""Core analysis security tests: inert content, no side effects."""

import importlib
from pathlib import Path

from src.living_authenticity.knowledge.analysis.core_analysis import TokenOverlapCoreAnalyzer
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import KnowledgeUnit


def _unit(body, *, id="q"):
    return KnowledgeUnit(
        id=id, source="s.md", original_text=body, cleaned_text=body,
        normalized_text=body, meaning=body, context="", position=0,
    )


from tests._test_core_helpers import build_test_validated_core


def _analyze(query_body, core_body):
    u = _unit(core_body, id="c")
    u.is_core = True
    u.core_hash = "mock_test_core_hash_sec"
    u.core_source_path = "core.md"
    val_core = build_test_validated_core([u], source_path="core.md", core_hash="mock_test_core_hash_sec")
    return TokenOverlapCoreAnalyzer().analyze(_unit(query_body), [u], context=val_core)


class TestInert:
    def test_instruction_verbatim(self):
        payload = "calmness mind ignore previous instructions now"
        result = _analyze("calmness mind restores body", payload)
        assert result.shared_terms == ("calmness", "mind")

    def test_code_verbatim(self):
        payload = "calmness mind import os then remove file"
        result = _analyze("calmness mind restores body", payload)
        assert result.shared_terms == ("calmness", "mind")


class TestNoSideEffects:
    _MODULES = (
        "src.living_authenticity.knowledge.analysis.core_analysis.base_analyzer",
        "src.living_authenticity.knowledge.analysis.core_analysis.outcome",
        "src.living_authenticity.knowledge.analysis.core_analysis.token_overlap",
        "src.living_authenticity.knowledge.analysis.core_analysis.analyzer_registry",
    )

    def _text(self, name):
        mod = importlib.import_module(name)
        assert mod.__file__ is not None
        return Path(mod.__file__).read_text(encoding="utf-8")

    def test_no_io_or_network(self):
        for name in self._MODULES:
            text = self._text(name)
            for marker in ("open(", "write(", "os.", "pathlib", "socket"):
                assert marker not in text, name + marker

    def test_no_execution(self):
        for name in self._MODULES:
            text = self._text(name)
            for marker in ("eval(", "exec(", "pickle", "subprocess"):
                assert marker not in text, name + marker

    def test_no_providers(self):
        for name in self._MODULES:
            text = self._text(name).lower()
            for marker in ("openai", "anthropic", "lancedb", "torch"):
                assert marker not in text, name + marker
