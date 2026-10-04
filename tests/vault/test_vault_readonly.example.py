"""Synthetic counterpart to tests/local/test_vault_readonly_local.py.

Proves the same bounded, read-only vault behaviour using a synthetic
vault directory, so the tracked suite touches no real vault and no
production data.
"""
from src.living_authenticity.knowledge.vault.context import (
    read_vault_context,
)
from src.living_authenticity.security.path_boundary import PathBoundary


def _snapshot(root):
    return sorted(
        (p.name, p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.iterdir()
    )


def test_synthetic_vault_read_is_bounded_and_leaves_no_residue(tmp_path):
    vault = tmp_path / "synthetic_vault"
    vault.mkdir()
    (vault / "a.md").write_text("Synthetic note A.", encoding="utf-8")
    (vault / "b.md").write_text("Synthetic note B.", encoding="utf-8")
    (vault / "c.txt").write_text("Not markdown.", encoding="utf-8")
    before = _snapshot(vault)

    excerpts, skipped = read_vault_context(str(vault))
    assert len(excerpts) == 2
    assert all(item["source"].endswith(".md") for item in excerpts)
    assert skipped == 0

    after = _snapshot(vault)
    assert after == before, "a read-only vault reader must not mutate the vault"
    assert sorted(p.name for p in vault.iterdir()) == [
        "a.md", "b.md", "c.txt"]


def test_synthetic_vault_respects_file_and_total_bounds(tmp_path):
    vault = tmp_path / "bounded_vault"
    vault.mkdir()
    (vault / "small.md").write_text("ok", encoding="utf-8")
    (vault / "big.md").write_text("x" * 5000, encoding="utf-8")
    excerpts, skipped = read_vault_context(
        str(vault), max_files=1, max_bytes_per_file=100,
        max_total_bytes=1000)
    assert len(excerpts) == 1
    assert skipped == 1


def test_synthetic_vault_boundary_refuses_escape(tmp_path):
    vault = tmp_path / "vault_escape"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    boundary = PathBoundary(str(vault))
    assert boundary.is_allowed(str(outside)) is False
    assert boundary.is_allowed(str(vault / "inside.md")) is True
