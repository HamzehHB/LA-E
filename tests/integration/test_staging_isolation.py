"""Staging-isolation tests for the bounded local-integration run (synthetic only)."""
import os
from pathlib import Path

import pytest

from Config.settings import ROOT as REPOSITORY_ROOT
from src.living_authenticity.security.path_boundary import PathBoundary
from src.living_authenticity.knowledge.integration import (
    GUARDED_PATH_KEYS,
    STAGING_PATH_KEY,
    check_staging_eligible,
    resolve_authorized_staging_root,
    resolve_guarded_roots,
    run_bounded_integration,
)
from tests._synthetic_synthesis import SyntheticSynthesis

SAMPLE = "Observation:\nPeople often lose calmness in modern life.\n"

CORE_SAMPLE = ("Method:\nA method for calibrating laboratory instruments "
               "with a step by step procedure.\n")


def _method_core():
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )

    return KnowledgeUnit(
        id="core1", source="core", original_text=CORE_SAMPLE,
        meaning=CORE_SAMPLE, cleaned_text=CORE_SAMPLE,
        normalized_text=CORE_SAMPLE,
        context="", proposed_type="", position=0,
    )


def _write(directory, name, text=SAMPLE):
    target = Path(directory) / name
    target.write_text(text, encoding="utf-8")
    return target


def _roots(tmp_path):
    guarded = str(tmp_path / "guarded")
    os.makedirs(guarded, exist_ok=True)
    staging = tmp_path / "staging"
    staging.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    return guarded, str(staging), str(source)


def _yes(_request=None):
    return "y"


def test_guarded_keys_cover_config_contract():
    assert GUARDED_PATH_KEYS == (
        "data.root", "models.bge_m3", "vector_db.lancedb", "memory.root",
        "obsidian.vault", "zotero.library", "exports.root", "cache.root",
    )


def test_resolve_guarded_roots_uses_example_values():
    roots = resolve_guarded_roots()
    assert len(roots) == 8
    assert all(isinstance(value, str) and value.strip() for value in roots)


def test_repository_root_staging_rejected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    report = run_bounded_integration(
        str(source), str(REPOSITORY_ROOT), guarded_roots=(),
        approval_reader=_yes)
    assert report.accepted is False
    assert report.units == ()
    assert list(tmp_path.iterdir()) == [source]


def test_repository_subdirectory_staging_rejected(tmp_path):
    from Config.settings import ROOT as ROOT_PATH

    assert PathBoundary(str(ROOT_PATH)).is_allowed(str(ROOT_PATH / "Config"))
    source = tmp_path / "source"
    source.mkdir()
    report = run_bounded_integration(
        str(source), str(ROOT_PATH / "Config"), guarded_roots=(),
        approval_reader=_yes)
    assert report.accepted is False


def test_isolated_tmp_staging_passes_isolation(tmp_path):
    _guarded, staging, source = _roots(tmp_path)
    eligible, _reason, _check = check_staging_eligible(staging, (_guarded,))
    assert eligible is True


def test_guarded_root_equality_rejected(tmp_path):
    guarded, _staging, source = _roots(tmp_path)
    eligible, _reason, check = check_staging_eligible(guarded, (guarded,))
    assert eligible is False
    assert check == "destination"


def test_guarded_subdirectory_rejected(tmp_path):
    guarded, _staging, source = _roots(tmp_path)
    inner = os.path.join(guarded, "inner")
    os.makedirs(inner, exist_ok=True)
    eligible, _reason, _check = check_staging_eligible(inner, (guarded,))
    assert eligible is False


def test_disjoint_staging_passes_guarded_check(tmp_path):
    guarded, staging, _source = _roots(tmp_path)
    eligible, _reason, _check = check_staging_eligible(staging, (guarded,))
    assert eligible is True


def test_trailing_slash_bypass_rejected(tmp_path):
    guarded, _staging, _source = _roots(tmp_path)
    eligible, _r, _c = check_staging_eligible(guarded + os.sep, (guarded,))
    assert eligible is False


def test_case_variant_bypass_rejected(tmp_path):
    guarded, _staging, _source = _roots(tmp_path)
    variant = guarded.swapcase()
    if variant == guarded:
        pytest.skip("filesystem case representation has no variant")
    allowed = PathBoundary(guarded).is_allowed(variant)
    eligible, _r, _c = check_staging_eligible(variant, (guarded,))
    assert eligible is (not allowed)


def test_relative_vs_absolute_bypass_rejected(tmp_path, monkeypatch):
    guarded, _staging, _source = _roots(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = os.path.join("guarded", ".", "inner-guard")
    os.makedirs(tmp_path / "guarded" / "inner-guard", exist_ok=True)
    assert PathBoundary(guarded).is_allowed(relative) is True
    eligible, _r, _c = check_staging_eligible(relative, (guarded,))
    assert eligible is False


def test_symlink_to_guarded_root_rejected(tmp_path):
    guarded, _staging, _source = _roots(tmp_path)
    link = tmp_path / "guard-link"
    try:
        link.symlink_to(guarded, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert PathBoundary(guarded).is_allowed(str(link)) is True
    eligible, _r, _c = check_staging_eligible(str(link), (guarded,))
    assert eligible is False


def test_symlink_staging_root_rejected_before_ingestion(tmp_path):
    guarded, staging, source = _roots(tmp_path)
    _write(source, "note.md")
    link = tmp_path / "stage-link"
    try:
        link.symlink_to(staging, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    report = run_bounded_integration(
        source, str(link), guarded_roots=(guarded,), approval_reader=_yes)
    assert report.accepted is False
    assert report.units == ()
    assert list(Path(staging).iterdir()) == []


# ---------------------------------------------------------------------------
# Narrow, config-declared staging exception inside a guarded persistent root.
# ---------------------------------------------------------------------------


def _exception_layout(tmp_path):
    """A guarded persistent root containing exactly one authorized staging dir."""
    data_root = tmp_path / "data-root"
    authorized = data_root / "Staging"
    authorized.mkdir(parents=True)
    (data_root / "Obsidian").mkdir()
    return str(data_root), str(authorized)


def test_authorized_staging_root_accepted(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    eligible, reason, check = check_staging_eligible(
        authorized, (data_root,), authorized)
    assert eligible is True
    assert reason == ""
    assert check == ""


def test_authorized_staging_root_with_trailing_separator_accepted(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    eligible, _r, _c = check_staging_eligible(
        authorized + os.sep, (data_root,), authorized)
    assert eligible is True


def test_authorized_staging_root_relative_form_accepted(tmp_path, monkeypatch):
    data_root, authorized = _exception_layout(tmp_path)
    monkeypatch.chdir(data_root)
    eligible, _r, _c = check_staging_eligible(
        os.path.join(".", "Staging"), (data_root,), authorized)
    assert eligible is True


def test_arbitrary_child_of_guarded_root_rejected(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    sibling = os.path.join(data_root, "SomeOtherArea")
    os.makedirs(sibling, exist_ok=True)
    eligible, reason, _c = check_staging_eligible(
        sibling, (data_root,), authorized)
    assert eligible is False
    assert "guarded persistent data" in reason or "authorized" in reason


def test_guarded_root_itself_rejected_even_with_authorization(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    eligible, _r, _c = check_staging_eligible(
        data_root, (data_root,), authorized)
    assert eligible is False


def test_subdirectory_of_authorized_staging_rejected(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    nested = os.path.join(authorized, "nested")
    os.makedirs(nested, exist_ok=True)
    eligible, reason, _c = check_staging_eligible(
        nested, (data_root,), authorized)
    assert eligible is False
    assert "outside the authorized staging location" in reason


def test_other_guarded_root_rejected_even_when_authorized_elsewhere(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    other_root = tmp_path / "other-guarded"
    other = other_root / "Staging"
    other.mkdir(parents=True)
    eligible, _r, _c = check_staging_eligible(
        str(other), (data_root, str(other_root)), authorized)
    assert eligible is False


def test_guarded_data_rejected_when_no_staging_declared(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    eligible, reason, _c = check_staging_eligible(authorized, (data_root,), "")
    assert eligible is False
    assert "guarded persistent data" in reason


def test_symlink_to_authorized_staging_rejected(tmp_path):
    data_root, authorized = _exception_layout(tmp_path)
    link = tmp_path / "authorized-link"
    try:
        link.symlink_to(authorized, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    eligible, reason, _c = check_staging_eligible(
        str(link), (data_root,), authorized)
    assert eligible is False
    assert reason == "staging root is a symlink"


def test_authorized_staging_root_resolved_from_config():
    value = resolve_authorized_staging_root()
    assert isinstance(value, str)
    assert value.strip() != ""
    assert STAGING_PATH_KEY == "staging.root"


def test_authorized_staging_inside_guarded_root_runs_end_to_end(tmp_path):
    """The declared exception works, and only for that exact directory."""
    data_root, authorized = _exception_layout(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    _write(source, "note.md")
    cores = [_method_core()]
    report = run_bounded_integration(
        str(source), authorized, guarded_roots=(data_root,),
        authorized_staging_root=authorized, approval_reader=_yes,
        core_units=cores, max_files=1, synthesis=SyntheticSynthesis())
    assert report.accepted is True
    assert report.staging_root == authorized
    assert any(unit.outcome == "executed" for unit in report.units)
    assert list(Path(authorized).iterdir()), "expected a staging artifact"

