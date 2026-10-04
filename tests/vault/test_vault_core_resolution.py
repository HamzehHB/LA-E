"""Vault Core resolution contract: schema-defined Core folder, read-only.

``Knowledge-Schema.yaml`` ``folder_structure`` designates ``01 Core`` as
the vault's authoritative Core folder. These tests prove the resolver
finds it when present, reports no Core otherwise, never escapes the vault
boundary, and mutates nothing.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.living_authenticity.knowledge.vault.context import (
    resolve_vault_core_root,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_resolves_schema_core_folder_without_mutation(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "01 Core" / "life.md", "core note")
    _write(vault / "03 Concept" / "attention.md", "concept note")
    before = sorted(
        (str(p.relative_to(vault)), p.read_text(encoding="utf-8"))
        for p in vault.rglob("*.md")
    )
    resolved = resolve_vault_core_root(str(vault))
    assert resolved
    assert Path(resolved).name == "01 Core"
    assert Path(resolved).parent == vault
    after = sorted(
        (str(p.relative_to(vault)), p.read_text(encoding="utf-8"))
        for p in vault.rglob("*.md")
    )
    assert before == after


def test_missing_core_folder_reports_no_core(tmp_path):
    vault = tmp_path / "vault"
    _write(vault / "03 Concept" / "attention.md", "concept note")
    assert resolve_vault_core_root(str(vault)) == ""


def test_non_directory_or_missing_root_reports_no_core(tmp_path):
    assert resolve_vault_core_root("") == ""
    assert resolve_vault_core_root(str(tmp_path / "absent")) == ""
    file_path = tmp_path / "note.md"
    _write(file_path, "not a directory")
    assert resolve_vault_core_root(str(file_path)) == ""


def test_core_folder_is_confined_to_the_configured_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    other = tmp_path / "other-vault"
    _write(other / "01 Core" / "life.md", "other core note")
    # A different vault's Core folder is not reachable from this root.
    assert resolve_vault_core_root(str(vault)) == ""


def test_folder_name_must_match_the_schema_definition(tmp_path):
    """The schema, not a hard-coded literal, decides the Core folder name."""
    from src.living_authenticity.knowledge.vault.context import (
        _schema_core_folder,
    )

    schema_folder = _schema_core_folder()
    assert schema_folder, "the tracked schema must define a Core folder"

    # A vault that does not follow the schema's folder name reports no
    # Core, proving the schema (not a literal) is the source of truth.
    wrong = tmp_path / "wrong"
    _write(wrong / "Core" / "note.md", "core note")
    assert resolve_vault_core_root(str(wrong)) == ""

    # A vault that follows the schema resolves.
    right = tmp_path / "right"
    _write(right / schema_folder / "note.md", "core note")
    assert resolve_vault_core_root(str(right)).endswith(schema_folder)
