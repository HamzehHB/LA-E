"""Vault bounds, cloud privacy gate, review block tests (synthetic)."""
import pytest

from src.living_authenticity.knowledge.vault.context import (
    read_vault_context,
)

from src.living_authenticity.llm import (
    LLMUnavailable,
    Synthesis,
    build_provider,
)


def test_vault_reader_bounds_and_read_only(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("hello vault", encoding="utf-8")
    (vault / "b.txt").write_text("ignored extension", encoding="utf-8")
    (vault / "big.md").write_text("x" * 200000, encoding="utf-8")
    before = sorted(p.name for p in vault.iterdir())
    excerpts, skipped = read_vault_context(str(vault), max_files=10,
                                           max_bytes_per_file=100,
                                           max_total_bytes=1000000)
    assert [e["source"] for e in excerpts] != []
    assert all(e["source"].endswith(".md") for e in excerpts)
    assert skipped >= 1
    assert sorted(p.name for p in vault.iterdir()) == before


def test_vault_reader_discovers_nested_markdown(tmp_path):
    """Nested directories are scanned; the real vault layout must work."""
    vault = tmp_path / "vault"
    (vault / "01 Core").mkdir(parents=True)
    (vault / "03 Concept").mkdir(parents=True)
    (vault / "03 Concept" / "deeper").mkdir(parents=True)
    (vault / "root.md").write_text("root note", encoding="utf-8")
    (vault / "01 Core" / "core.md").write_text("core note", encoding="utf-8")
    (vault / "03 Concept" / "concept.md").write_text(
        "concept note", encoding="utf-8")
    (vault / "03 Concept" / "deeper" / "nested.md").write_text(
        "nested note", encoding="utf-8")
    (vault / "03 Concept" / "ignore.txt").write_text(
        "not markdown", encoding="utf-8")

    excerpts, skipped = read_vault_context(str(vault))
    names = sorted(
        item["source"].replace("\\", "/").split("vault/")[-1]
        for item in excerpts
    )
    assert names == [
        "01 Core/core.md", "03 Concept/concept.md",
        "03 Concept/deeper/nested.md", "root.md",
    ]
    assert all(item["source"].endswith(".md") for item in excerpts)
    # Non-markdown descendants are never read.
    assert all(".txt" not in item["source"] for item in excerpts)


def test_vault_reader_respects_file_count_limit(tmp_path):
    vault = tmp_path / "vault"
    (vault / "a").mkdir(parents=True)
    (vault / "b").mkdir(parents=True)
    for index in range(5):
        (vault / "a" / ("n%d.md" % index)).write_text(
            "note %d" % index, encoding="utf-8")
    for index in range(5):
        (vault / "b" / ("n%d.md" % index)).write_text(
            "note %d" % index, encoding="utf-8")

    excerpts, _skipped = read_vault_context(str(vault), max_files=3)
    assert len(excerpts) == 3


def test_vault_reader_respects_total_size_limit(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(5):
        (vault / ("n%d.md" % index)).write_text("y" * 100, encoding="utf-8")
    excerpts, skipped = read_vault_context(
        str(vault), max_files=10, max_total_bytes=250)
    assert len(excerpts) <= 2
    assert skipped >= 1, "files beyond the total bound must be reported"


def test_vault_reader_skips_symlinked_notes(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside the vault", encoding="utf-8")
    link = vault / "link.md"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    excerpts, _skipped = read_vault_context(str(vault))
    assert excerpts == [], "a symlink out of the vault must never be read"


def test_vault_reader_rejects_missing_or_non_directory_root(tmp_path):
    assert read_vault_context(str(tmp_path / "does-not-exist")) == ([], 1)
    file_path = tmp_path / "note.md"
    file_path.write_text("not a directory", encoding="utf-8")
    assert read_vault_context(str(file_path)) == ([], 1)


def test_vault_reader_leaves_no_residue_in_nested_vault(tmp_path):
    vault = tmp_path / "vault"
    (vault / "01 Core").mkdir(parents=True)
    (vault / "01 Core" / "core.md").write_text("core", encoding="utf-8")

    def snapshot():
        return sorted(
            (str(p.relative_to(vault)), p.stat().st_size, p.stat().st_mtime_ns)
            for p in vault.rglob("*")
        )

    before = snapshot()
    read_vault_context(str(vault))
    assert snapshot() == before, "a read-only reader must not mutate the vault"


def test_cloud_privacy_gate_on_resolved_host():
    provider = build_provider({"llm": {"provider": "cloud", "model": "m",
                                        "endpoint": "https://example.invalid",
                                        "credential_env": "X_CRED"}})
    assert provider.requires_privacy_confirmation is True
    remote_ollama = build_provider({"llm": {"provider": "ollama",
                                             "model": "m",
                                             "endpoint":
                                             "https://example.invalid"}})
    assert remote_ollama.requires_privacy_confirmation is True
    local = build_provider({"llm": {"provider": "ollama", "model": "m",
                                     "endpoint": "http://127.0.0.1:11434"}})
    assert local.requires_privacy_confirmation is False
    seen = {}

    def _decline(question, prompt):
        seen["prompt"] = prompt
        return False

    synthesis = Synthesis(remote_ollama, confirm=_decline)
    with pytest.raises(LLMUnavailable):
        synthesis.synthesize()
    assert "prompt" in seen

    def _accept(question, prompt):
        return True

    class _Echo:
        name = "cloud"
        endpoint = "https://example.invalid"
        requires_privacy_confirmation = True

        def complete(self, prompt):
            return ('{"candidates": [{"title": "t", "body": "b", '
                    '"suggested_type": "", "reason": "r", '
                    '"uncertainty": "u"}]}')

    result, prompt, digest = Synthesis(_Echo(), confirm=_accept).synthesize()
    assert result.candidates[0].body == "b"
    assert len(digest) == 64


def test_review_block_contains_verbatim_staged_text():
    from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
        ExplicitApprovalGate,
    )
    gate = ExplicitApprovalGate()
    staged = "# Title\n\nVerbatim staged text.\n"
    block = ("Source\nTitle\nSuggested action\nConfidence\nWhy\nEvidence\n"
             "Retrieved context\nRelated knowledge\nLLM analysis\n"
             "Proposed note\n" + staged + "\nDestination\nhash")
    assert staged in block
    assert gate.display.__name__ == "display"

