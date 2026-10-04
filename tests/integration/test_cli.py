"""Tests for the portable command-line entry point (no writes, no defaults)."""
from Config.settings import ROOT as REPOSITORY_ROOT
from src.living_authenticity.cli import build_parser, main


def test_parser_requires_roots_and_vector_pairing():
    parser = build_parser()
    parsed = parser.parse_args(
        ["--source-root", "src", "--staging-root", "out",
         "--corpus-root", "corp", "--core-root", "core",
         "--embed", "--vector-db", "vdb", "--max-files", "3"])
    assert parsed.source_root == "src"
    assert parsed.staging_root == "out"
    assert parsed.corpus_root == "corp"
    assert parsed.core_root == "core"
    assert parsed.embed is True
    assert parsed.vector_db == "vdb"
    assert parsed.max_files == 3


def test_parser_defaults_have_no_discovery():
    parsed = build_parser().parse_args(
        ["--source-root", "src", "--staging-root", "out"])
    assert parsed.corpus_root == ""
    assert parsed.core_root == ""
    assert parsed.vector_db == ""
    assert parsed.embed is False
    assert parsed.max_files == 1


def test_missing_input_mode_exits_with_usage_error(capsys):
    """No input mode at all is a usage error (exit 2), not a run."""
    parser = build_parser()
    parsed = parser.parse_args(["--source-root", "src"])
    assert parsed.input_file == ""
    assert parsed.text == ""
    code = main([])
    assert code == 2
    captured = capsys.readouterr().out
    assert "exactly one of --input-file, --text, --source-root" in captured


def test_input_file_and_text_are_mutually_exclusive(capsys):
    parser = build_parser()
    parsed = parser.parse_args(["--input-file", "a.md", "--text", "b"])
    assert parsed.input_file == "a.md"
    assert parsed.text == "b"
    code = main(["--input-file", "a.md", "--text", "b"])
    assert code == 2
    assert "exactly one of" in capsys.readouterr().out


def test_main_reports_rejection_without_writing(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    code = main([
        "--source-root", str(source),
        "--staging-root", str(REPOSITORY_ROOT),
    ])
    assert code == 2
    captured = capsys.readouterr().out
    assert "staging rejected" in captured
    assert list(tmp_path.iterdir()) == [source]


def test_main_rejects_invalid_max_files(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    code = main([
        "--source-root", str(source),
        "--staging-root", str(staging),
        "--max-files", "0",
    ])
    assert code == 2
    assert "max-files must be a positive integer" in capsys.readouterr().out
    assert list(staging.iterdir()) == []


def test_main_rejects_embed_without_vector_db(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    code = main([
        "--source-root", str(source), "--staging-root", str(staging),
        "--embed",
    ])
    assert code == 2
    assert "requires an explicit --vector-db" in capsys.readouterr().out
    assert list(staging.iterdir()) == []


def test_main_rejects_vector_db_without_embed(tmp_path, capsys):
    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    code = main([
        "--source-root", str(source), "--staging-root", str(staging),
        "--vector-db", str(tmp_path / "vdb"),
    ])
    assert code == 2
    assert "only used together with --embed" in capsys.readouterr().out
    assert list(staging.iterdir()) == []


def test_main_rejects_production_vector_db(tmp_path, capsys, monkeypatch):
    import src.living_authenticity.cli as cli_module

    source = tmp_path / "source"
    source.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir()
    production = tmp_path / "production-vdb"
    production.mkdir()

    monkeypatch.setattr(cli_module, "load_paths", lambda: {
        "models": {"bge_m3": str(tmp_path / "model")},
        "vector_db": {"lancedb": str(production)},
    })
    code = main([
        "--source-root", str(source), "--staging-root", str(staging),
        "--embed", "--vector-db", str(production),
    ])
    assert code == 2
    captured = capsys.readouterr().out
    assert "production vector database" in captured
    assert list(staging.iterdir()) == []

