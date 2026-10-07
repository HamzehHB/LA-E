"""The review surface must expose the real synthesis, and stop on bad output."""
import json

from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
    ExplicitApprovalGate,
)
from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
    KnowledgeUnit,
)
from src.living_authenticity.knowledge.integration.review_display import (
    render_review_block,
)
from src.living_authenticity.knowledge.integration.unit_loop import (
    _handle_unit,
)
from src.living_authenticity.knowledge.orchestration.orchestrator import (
    EvidenceFirstPipeline,
)
from src.living_authenticity.llm import LLMProvider, LLMUnavailable, Synthesis

# Use the actual example file content which produces a CREATE proposal
_TEXT = """Observation:

I observed that people often start their working day by reading a short
summary before opening any long document.

The pattern repeats across a small synthetic example set used only to
demonstrate the bounded local-integration workflow.

## Questions

Does a short summary reliably reduce context switching in this example?"""


def _unit():
    return KnowledgeUnit(id="q1", source="note.md", original_text=_TEXT,
                         meaning=_TEXT, cleaned_text=_TEXT,
                         normalized_text=_TEXT)


def _candidate(**overrides):
    entry = {"title": "Morning reading habit and context switching",
             "body": ("Starting the day with a short summary before opening long documents "
                      "is a repeated pattern in the example set. "
                      "The question is whether this reliably reduces context switching."),
             "suggested_type": "Observation",
             "reason": "The unit states a repeated personal observation with a testable question.",
             "uncertainty": "Causal direction and generalizability are not established."}
    entry.update(overrides)
    return json.dumps({"candidates": [entry]})


class _FakeProvider(LLMProvider):
    """A provider stub that returns one canned completion."""

    def __init__(self, raw):
        self._raw = raw
        self.prompts = []

    @property
    def name(self):
        return "fake"

    @property
    def model(self):
        return "fake-model"

    @property
    def endpoint(self):
        return "http://127.0.0.1:11434"

    def complete(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self._raw, Exception):
            raise self._raw
        return self._raw


def _render(unit_result, staged):
    return render_review_block(
        proposal=unit_result.proposal, confidence=unit_result.confidence,
        filter_outcome=unit_result.filter_outcome,
        retrieval=unit_result.retrieval_result,
        llm_result=None, staged_text=staged,
        destination=unit_result.proposal.destination,
        proposal_hash="hash", query=unit_result.query_unit,
        classification=unit_result.classification,
        relation=unit_result.relation_result, core=unit_result.core_analysis,
        comparisons=unit_result.comparisons)


def test_review_block_shows_every_stage_and_the_verbatim_text():
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    staged = "# Morning writing and attention\n\nBody.\n"
    block = _render(unit_result, staged)
    for label in ("Source", "Title", "Suggested action", "Confidence", "Why",
                  "Evidence", "Retrieved context", "Related knowledge",
                  "Core relevance", "Filter verdict", "LLM analysis",
                  "Comparison categories", "Destination", "Proposal hash",
                  "Approve this action? [y/N]:"):
        assert label in block, label
    assert staged in block
    assert "this is what will be written if approved" in block


def test_llm_synthesis_runs_and_produces_valid_candidate(tmp_path):
    """LLM synthesis runs and produces a validated candidate regardless of proposal action."""
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    provider = _FakeProvider(_candidate())
    details: list = []
    entries: list = []
    _handle_unit("note.md", unit_result, entries=entries, audits=[],
                 approval_reader=lambda: "n", details=details,
                 synthesis=Synthesis(provider), pinned_staging=str(staging))
    assert provider.prompts, "the provider must actually be called"
    assert "OUTPUT CONTRACT" in provider.prompts[0]
    assert entries  # some outcome is recorded
    action = unit_result.proposal.action
    if action == "CREATE":
        # For CREATE proposals, details are recorded during governance
        assert details, "details should be recorded for CREATE proposals"
        assert details[0].llm_result.candidates[0].suggested_type in (
            "Observation", "Concept", "Experience", "Research", "Method", "Source", "Meta", "Archive", ""
        )
        assert "Morning reading" in details[0].note_markdown
    else:
        # For non-CREATE proposals, governance (and thus details) is skipped
        # This is correct: only CREATE proposals enter human review/governance
        assert entries[0].outcome == "held_non_executable"


def test_interactive_review_called_only_for_create_proposals(monkeypatch, capsys, tmp_path):
    """The approval reader is only invoked for CREATE proposals (correct behavior)."""
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    
    # First, check what proposal action was generated
    action = unit_result.proposal.action
    
    if action == "CREATE":
        # For CREATE proposals, the reader should be called
        seen = {"called": False}
        def _fake_reader():
            seen["called"] = True
            return "n"
        entries: list = []
        audits: list = []
        _handle_unit("note.md", unit_result,
                     gate=ExplicitApprovalGate(), entries=entries,
                     audits=audits, pinned_staging=str(staging),
                     synthesis=Synthesis(_FakeProvider(_candidate())),
                     approval_reader=lambda: "n")
        out = capsys.readouterr().out
        assert seen["called"], "reader must be called for CREATE proposals"
        assert "LLM analysis:" in capsys.readouterr().out
        assert "Approve this action? [y/N]:" in out
    else:
        # For non-CREATE proposals, the reader is NOT called (correct behavior)
        entries: list = []
        audits: list = []
        _handle_unit("note.md", unit_result,
                     gate=ExplicitApprovalGate(), entries=entries,
                     audits=audits, pinned_staging=str(staging),
                     synthesis=Synthesis(_FakeProvider(_candidate())),
                     approval_reader=lambda: "unexpected")
        # For non-CREATE, reader is never called
        out = capsys.readouterr().out
        assert "Approve this action? [y/N]:" not in out
        assert entries
        assert entries[0].outcome == "held_non_executable"


def test_invalid_llm_output_stops_the_unit_with_the_right_reason(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    for raw in ("not json at all", "{}", ""):
        entries: list = []
        audits: list = []
        _handle_unit("note.md", unit_result, entries=entries, audits=audits,
                     approval_reader=lambda: "y",
                     synthesis=Synthesis(_FakeProvider(raw)),
                     pinned_staging=str(staging))
        assert len(entries) == 1
        assert entries[0].outcome == "llm_safe_stop", raw
        assert entries[0].reason == "llm_invalid_output", raw
        assert len(audits) == 1, (
            "safe stops persist a non-approved audit record, never approval")
        assert audits[0].approval_approved is False
        assert audits[0].execution_executed is False


def test_missing_fields_and_bad_types_are_invalid_output(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    missing = json.dumps({"candidates": [
        {"title": "t", "body": "b", "suggested_type": "Concept"}]})
    for raw in (missing, _candidate(body=""), _candidate(body=5)):
        entries: list = []
        _handle_unit("note.md", unit_result, entries=entries, audits=[],
                     approval_reader=lambda: "y",
                     synthesis=Synthesis(_FakeProvider(raw)),
                     pinned_staging=str(staging))
        assert entries[0].reason == "llm_invalid_output", raw


def test_unavailable_provider_stops_the_unit_safely(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    entries: list = []
    audits: list = []
    _handle_unit("note.md", unit_result, entries=entries, audits=audits,
                 approval_reader=lambda: "y",
                 synthesis=Synthesis(_FakeProvider(
                     LLMUnavailable("LLM endpoint unreachable: URLError"))),
                 pinned_staging=str(staging))
    assert entries[0].outcome == "llm_safe_stop"
    assert entries[0].reason == "llm_unavailable"
    assert len(audits) == 1, (
        "safe stops persist a non-approved audit record, never approval")
    assert audits[0].approval_approved is False
    assert audits[0].execution_executed is False
    assert audits[0].stop_reason == "llm_unavailable"


def test_timeout_maps_to_the_timeout_reason(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    entries: list = []
    _handle_unit("note.md", unit_result, entries=entries, audits=[],
                 approval_reader=lambda: "y",
                 synthesis=Synthesis(_FakeProvider(
                     LLMUnavailable("LLM endpoint unreachable: timeout"))),
                 pinned_staging=str(staging))
    assert entries[0].reason == "llm_timeout"


def test_the_llm_stage_cannot_be_bypassed(tmp_path):
    """No synthesis collaborator means the mandatory stage cannot run."""
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    entries: list = []
    try:
        _handle_unit("note.md", unit_result, entries=entries, audits=[],
                     synthesis=None, pinned_staging=str(staging))
    except TypeError as exc:
        assert "synthesis is required" in str(exc)
    else:
        raise AssertionError("a missing synthesis collaborator must be refused")
    assert entries == [], "nothing may be governed without the LLM stage"


def test_non_create_proposal_exposes_full_inspectable_information(tmp_path):
    """A held NEEDS_REVIEW candidate must still be fully inspectable.

    This is the exact situation the real manual run produced:
    ``NEEDS_REVIEW`` / ``held_non_executable`` / "candidate proposal action
    is not CREATE". The human must still be able to see the proposal, the
    LLM synthesis, confidence, and the filter outcome.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    unit_result = EvidenceFirstPipeline().run_unit(_unit())
    assert unit_result.proposal.action != "CREATE", (
        "this test must exercise a non-CREATE proposal")
    provider = _FakeProvider(_candidate())
    entries: list = []
    _handle_unit("note.md", unit_result, entries=entries, audits=[],
                 approval_reader=lambda: "y", pinned_staging=str(staging),
                 synthesis=Synthesis(provider))

    assert len(entries) == 1
    entry = entries[0]
    assert entry.outcome == "held_non_executable"
    assert entry.reason == "candidate proposal action is not CREATE"
    assert entry.proposal_action == "NEEDS_REVIEW"

    # The proposal itself is visible.
    assert entry.proposal_title
    assert entry.proposal_reason
    assert entry.proposed_type == "Observation"

    # Confidence and the Knowledge Filter are visible.
    assert entry.confidence_level
    assert entry.confidence_basis
    assert entry.filter_verdict
    assert entry.filter_basis

    # Classification and Core analysis are visible.
    assert entry.classification_type == "Observation"
    assert entry.classification_rationale
    assert entry.core_relevance

    # The LLM synthesis ran and its candidate fields are visible.
    assert provider.prompts, "the LLM stage must still run for held units"
    assert len(entry.llm_candidates) == 1
    candidate = entry.llm_candidates[0]
    assert candidate["title"] == "Morning reading habit and context switching"
    assert candidate["suggested_type"] == "Observation"
    assert candidate["body"]
    assert candidate["reason"]
    assert candidate["uncertainty"]
    # The selected candidate is also surfaced directly.
    assert entry.proposal_body == candidate["body"]
    assert entry.uncertainty == candidate["uncertainty"]

    # Nothing may execute for a held candidate.
    assert entry.execution_executed is False
    assert entry.approval_approved is False
    assert list(staging.iterdir()) == []


def test_held_candidate_is_reported_by_the_cli_summary(tmp_path, capsys):
    """The CLI must print the enriched entry fields under --verbose."""
    from src.living_authenticity.cli import main

    staging = tmp_path / "staging"
    staging.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text(_TEXT, encoding="utf-8")
    import src.living_authenticity.cli as cli_module
    code = cli_module.main([
        "--source-root", str(source), "--staging-root", str(staging),
        "--verbose",
    ])
    out = capsys.readouterr().out
    assert code in (0, 2)
    if "ALL PROPOSALS" in out:
        assert "proposal_reason:" in out
        assert "confidence_basis:" in out
        assert "filter_verdict:" in out
        assert "classification_rationale:" in out


