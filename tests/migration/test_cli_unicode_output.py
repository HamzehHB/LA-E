"""CLI Unicode-output contract: legacy consoles must not crash on Unicode.

The bounded-integration CLI prints legitimate Unicode text (Persian and
other non-Latin characters) coming from Vault notes, LLM titles/bodies,
and proposed notes. On a Windows console whose text stream uses a legacy
code page such as cp1252, that raised ``UnicodeEncodeError``. The CLI now
switches its output streams to UTF-8; these tests prove the crash is gone
and that no character is dropped.
"""
import io

from src.living_authenticity.cli import _configure_unicode_output

PERSIAN = "\u06cc\u0627\u062f\u06af\u06cc\u0631\u06cc"  # "یادگیری"
TURKISH_S = "\u015f"  # "ş"


def test_legacy_console_stream_no_longer_crashes_on_unicode():
    buffer = io.BytesIO()
    legacy = io.TextIOWrapper(buffer, encoding="cp1252")
    # cp1252 cannot encode Persian: this is exactly the observed failure.
    _configure_unicode_output((legacy,))
    legacy.write(PERSIAN + " " + TURKISH_S)
    legacy.flush()
    written = buffer.getvalue().decode("utf-8")
    assert PERSIAN in written
    assert TURKISH_S in written


def test_stream_without_reconfigure_is_left_untouched():
    class _Plain:
        def __init__(self):
            self.chunks = []

        def write(self, text):
            self.chunks.append(text)

    plain = _Plain()
    # Must not raise when a stream exposes no ``reconfigure``.
    _configure_unicode_output((plain,))
    plain.write(PERSIAN)
    assert plain.chunks == [PERSIAN]
