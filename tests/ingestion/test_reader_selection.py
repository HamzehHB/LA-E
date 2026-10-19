import pytest

from src.living_authenticity.knowledge.ingestion.readers.base_reader import BaseReader
from src.living_authenticity.knowledge.ingestion.readers.chat_input_reader import (
    ChatInputReader,
)
from src.living_authenticity.knowledge.ingestion.readers.markdown_reader import (
    MarkdownReader,
)
from src.living_authenticity.knowledge.ingestion.readers.reader_registry import (
    ReaderRegistry,
)
from src.living_authenticity.knowledge.ingestion.readers.txt_reader import TXTReader


class TestReaderRegistry:
    def test_default_readers_registered(self):
        reg = ReaderRegistry()
        assert isinstance(reg.get(".md"), MarkdownReader)
        assert isinstance(reg.get("note.md"), MarkdownReader)
        assert isinstance(reg.get(".txt"), TXTReader)
        assert isinstance(reg.get("document.txt"), TXTReader)
        assert isinstance(reg.get("chat_input"), ChatInputReader)
        assert isinstance(reg.get("text"), TXTReader)

    def test_supported_formats_lists_canonical_keys(self):
        reg = ReaderRegistry()
        formats = reg.supported_formats
        assert ".md" in formats
        assert ".txt" in formats
        assert "chat_input" in formats
        assert "text" in formats

    def test_unsupported_formats_fail_fast(self):
        reg = ReaderRegistry()
        for unsupported in ("doc.pdf", ".pdf", "photo.png", "audio.mp3", "notes.docx", "unknown_format"):
            with pytest.raises(ValueError, match="No reader registered"):
                reg.get(unsupported)

    def test_custom_registration_works(self):
        class DummyReader(BaseReader):
            def read(self, file_path: str) -> str:
                return "dummy"

        reg = ReaderRegistry()
        dummy = DummyReader()
        reg.register(".custom", dummy)
        assert reg.get("file.custom") is dummy
        assert reg.get(".CUSTOM") is dummy


class TestChatInputReader:
    def test_read_text_returns_exact_text(self):
        reader = ChatInputReader()
        sample = "مشاهده مطرح‌شده:\nبررسی دقیق فرآیند ورود داده و پالایش آن."
        assert reader.read_text(sample) == sample

    def test_read_text_strips_utf8_bom(self):
        reader = ChatInputReader()
        sample = "\ufeffمشاهده:\nمتن ورودی با علامت BOM."
        result = reader.read_text(sample)
        assert result == "مشاهده:\nمتن ورودی با علامت BOM."
        assert not result.startswith("\ufeff")

    def test_read_text_rejects_non_string(self):
        reader = ChatInputReader()
        with pytest.raises(TypeError, match="text must be a string"):
            reader.read_text(12345)  # type: ignore

    def test_read_file_handles_utf8_and_bom(self, tmp_path):
        reader = ChatInputReader()
        file_path = tmp_path / "sample.txt"
        file_path.write_bytes("\ufeffسلام دنیا".encode("utf-8"))
        read_back = reader.read(str(file_path))
        assert read_back == "سلام دنیا"
        assert "\ufeff" not in read_back


class TestRegistryConsistency:
    """Rigorous parameterized tests verifying case normalization, dot-prefix normalization,
    file paths, supported identifiers, and explicit fail-fast behavior across Reader, Chunker,
    and Extractor registries."""

    from src.living_authenticity.knowledge.ingestion.chunking.chunker_registry import (
        ChunkerRegistry,
    )
    from src.living_authenticity.knowledge.ingestion.chunking.markdown_chunker import (
        MarkdownChunker,
    )
    from src.living_authenticity.knowledge.ingestion.chunking.paragraph_chunker import (
        ParagraphChunker,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.extractor_registry import (
        ExtractorRegistry,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.markdown_extractor import (
        MarkdownKnowledgeExtractor,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.plain_text_extractor import (
        PlainTextKnowledgeExtractor,
    )

    @pytest.mark.parametrize("registry_cls,md_type,txt_type,chat_type", [
        (ReaderRegistry, MarkdownReader, TXTReader, ChatInputReader),
        (ChunkerRegistry, MarkdownChunker, ParagraphChunker, MarkdownChunker),
        (ExtractorRegistry, MarkdownKnowledgeExtractor, PlainTextKnowledgeExtractor, MarkdownKnowledgeExtractor),
    ])
    def test_symmetric_canonical_identifiers(self, registry_cls, md_type, txt_type, chat_type):
        reg = registry_cls()
        assert isinstance(reg.get(".md"), md_type)
        assert isinstance(reg.get(".txt"), txt_type)
        assert isinstance(reg.get("chat_input"), chat_type)
        assert isinstance(reg.get("text"), txt_type)

    @pytest.mark.parametrize("registry_cls,expected_type", [
        (ReaderRegistry, MarkdownReader),
        (ChunkerRegistry, MarkdownChunker),
        (ExtractorRegistry, MarkdownKnowledgeExtractor),
    ])
    @pytest.mark.parametrize("identifier", [
        ".md", ".MD", ".Md", "md", "MD",
        "note.md", "research.MD", "sub/folder/note.md", "C:\\vault\\entry.md"
    ])
    def test_markdown_identifiers_and_paths(self, registry_cls, expected_type, identifier):
        reg = registry_cls()
        assert isinstance(reg.get(identifier), expected_type)

    @pytest.mark.parametrize("registry_cls,expected_type", [
        (ReaderRegistry, TXTReader),
        (ChunkerRegistry, ParagraphChunker),
        (ExtractorRegistry, PlainTextKnowledgeExtractor),
    ])
    @pytest.mark.parametrize("identifier", [
        ".txt", ".TXT", ".Txt", "txt", "TXT", "text", "TEXT", "Text",
        "document.txt", "README.TXT", "sub/folder/log.txt", "D:\\data\\plain.txt"
    ])
    def test_text_identifiers_and_paths(self, registry_cls, expected_type, identifier):
        reg = registry_cls()
        assert isinstance(reg.get(identifier), expected_type)

    @pytest.mark.parametrize("registry_cls,expected_type", [
        (ReaderRegistry, ChatInputReader),
        (ChunkerRegistry, MarkdownChunker),
        (ExtractorRegistry, MarkdownKnowledgeExtractor),
    ])
    @pytest.mark.parametrize("identifier", [
        "chat_input", "CHAT_INPUT", "Chat_Input"
    ])
    def test_chat_input_case_normalization(self, registry_cls, expected_type, identifier):
        reg = registry_cls()
        assert isinstance(reg.get(identifier), expected_type)

    @pytest.mark.parametrize("registry_cls,match_regex", [
        (ReaderRegistry, "No reader registered for"),
        (ChunkerRegistry, "No chunker registered for"),
        (ExtractorRegistry, "No knowledge unit extractor registered for"),
    ])
    @pytest.mark.parametrize("unsupported", [
        ".pdf", "document.pdf", "DOCUMENT.PDF",
        ".docx", "word.docx",
        ".png", "image.png",
        ".mp3", "audio.mp3",
        ".csv", "data.csv",
        ".xlsx", "sheet.xlsx",
        "unknown_custom_format", "foo.bar"
    ])
    def test_unsupported_formats_fail_fast_across_all_registries(self, registry_cls, match_regex, unsupported):
        reg = registry_cls()
        with pytest.raises(ValueError, match=match_regex):
            reg.get(unsupported)
