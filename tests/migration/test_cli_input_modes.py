"""CLI input modes, hash coverage, and byte-identity tests (synthetic)."""
import hashlib

from src.living_authenticity.cli import build_parser, main


def test_input_modes_are_mutually_exclusive():
    """Mode selection is a pure usage check: no config, no run."""
    parser = build_parser()
    args = parser.parse_args(["--text", "hello"])
    assert args.text == "hello"
    assert args.input_file == ""
    assert args.source_root == ""
    assert "--no-llm" not in parser.format_help()

    # Neither mode is a usage error.
    assert main([]) == 2
    # Two modes at once is a usage error.
    assert main(["--input-file", "a.md", "--text", "b"]) == 2
    # Both checks happen before any configuration is read or any run
    # starts, so no staging root, no vault, and no provider is touched.


def test_hash_covers_displayed_note_body():
    from src.living_authenticity.knowledge.decision.confidence.outcome import (
        ConfidenceAssessment,
    )
    from src.living_authenticity.knowledge.decision.proposal.outcome import (
        Proposal,
    )
    from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
        ExplicitApprovalGate,
    )
    from src.living_authenticity.knowledge.governance.revalidation.revalidator import (
        Revalidator,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )
    from src.living_authenticity.knowledge.output.outcome import GeneratedNote

    query = KnowledgeUnit(id="q1", source="s", original_text="hello world x",
                          meaning="hello world x")
    proposal = Proposal(query_unit_id="q1", query_source="s",
                        query_position=0, action="CREATE", title="t",
                        provenance=("query:q1@s#0",))
    note = GeneratedNote(query_unit_id="q1", query_source="s",
                         query_position=0, markdown="# t\n\nbody\n",
                         proposal_action="CREATE")
    gate = ExplicitApprovalGate()
    request = gate.build_request(query, proposal, None, note)
    outcome = gate.decide(request, "y")
    assert Revalidator().revalidate(
        request, outcome, proposal, None, "", note).valid is True
    assert request.note_bound is True
    mutated = GeneratedNote(query_unit_id="q1", query_source="s",
                            query_position=0, markdown="# t\n\nbodx\n",
                            proposal_action="CREATE")
    failed = Revalidator().revalidate(
        request, outcome, proposal, None, "", mutated)
    assert failed.valid is False
    assert failed.failed_check == "proposal_identity"


def test_byte_identity_display_equals_written(tmp_path):
    from src.living_authenticity.knowledge.decision.proposal.outcome import (
        Proposal,
    )
    from src.living_authenticity.knowledge.governance.approval.explicit_gate import (
        ExplicitApprovalGate,
    )
    from src.living_authenticity.knowledge.governance.execution.executor import (
        ControlledExecutor,
    )
    from src.living_authenticity.knowledge.ingestion.extraction.knowledge_unit import (
        KnowledgeUnit,
    )
    from src.living_authenticity.knowledge.output.outcome import GeneratedNote

    staging = tmp_path / "staging"
    staging.mkdir()
    query = KnowledgeUnit(id="q2", source="s", original_text="hello world x",
                          meaning="hello world x")
    proposal = Proposal(query_unit_id="q2", query_source="s",
                        query_position=0, action="CREATE", title="t",
                        provenance=("query:q2@s#0",))
    note = GeneratedNote(query_unit_id="q2", query_source="s",
                         query_position=0, markdown="# t\n\nbody\n",
                         proposal_action="CREATE")
    gate = ExplicitApprovalGate()
    request = gate.build_request(query, proposal, None, note)
    outcome = gate.decide(request, "y")
    result = ControlledExecutor().execute(
        request, outcome, proposal, None, note,
        staging_root=str(staging))
    assert result.executed is True
    written = (staging / (request.proposal_hash + ".md")).read_text(
        encoding="utf-8")
    assert written == note.markdown
    assert hashlib.sha256(
        written.encode("utf-8")).hexdigest() != hashlib.sha256(
            b"# t\n\nbodx\n").hexdigest()
