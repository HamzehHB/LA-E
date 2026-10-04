"""Vault reader contract: recursive, bounded, read-only, inside root."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.vault.context import (
    discover_vault_files,
    read_vault_context,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_discovers_nested_markdown_and_ignores_other_suffixes(tmp_path):
    _write(tmp_path / "01 Core" / "note-a.md", "alpha note")
    _write(tmp_path / "02 Observation" / "nested" / "note-b.md", "beta note")
    _write(tmp_path / "02 Observation" / "skip.txt", "not markdown")
    files, skipped = discover_vault_files(str(tmp_path), max_files=10)
    names = sorted(p.name for p in files)
    assert names == ["note-a.md", "note-b.md"]
    assert skipped == 0


def test_respects_count_size_and_total_limits(tmp_path):
    _write(tmp_path / "a.md", "x" * 10)
    _write(tmp_path / "b.md", "y" * 10)
    _write(tmp_path / "c.md", "z" * 10)
    files, skipped = discover_vault_files(str(tmp_path), max_files=2)
    assert len(files) == 2
    assert skipped >= 1
    files, skipped = discover_vault_files(
        str(tmp_path), max_files=10, max_bytes_per_file=5)
    assert files == []
    assert skipped == 3
    files, skipped = discover_vault_files(
        str(tmp_path), max_files=10, max_total_bytes=15)
    assert len(files) == 1
    assert skipped == 2


def test_stays_inside_root_and_is_read_only(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "note.md", "original content")
    before = sorted(p.read_text(encoding="utf-8") for p in vault.rglob("*")
                    if p.is_file())
    files, _ = discover_vault_files(str(vault))
    assert len(files) == 1
    outside = tmp_path / "outside.md"
    _write(outside, "outside")
    # Traversal-styled input must not escape the configured root.
    files, skipped = discover_vault_files(
        str(vault / ".." / "vault"), max_files=10)
    assert all(str(vault) in str(p) for p in files)
    after = sorted(p.read_text(encoding="utf-8") for p in vault.rglob("*")
                   if p.is_file())
    assert before == after
    excerpts, _ = read_vault_context(str(vault))
    assert excerpts and excerpts[0]["excerpt"] == "original content"
    assert all("source" in item and "excerpt" in item for item in excerpts)
    # Reading changed nothing.
    after_again = sorted(p.read_text(encoding="utf-8") for p in vault.rglob("*")
                         if p.is_file())
    assert after == after_again


def test_missing_root_reports_skipped_without_exception(tmp_path):
    files, skipped = discover_vault_files(str(tmp_path / "absent"))
    assert files == []
    assert skipped == 1
    excerpts, skipped = read_vault_context("")
    assert excerpts == []
    assert skipped == 1
