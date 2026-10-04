"""Per-unit governance loop for the bounded local-integration run driver."""
from .guard import check_staging_eligible
from .outcome import IntegrationUnitEntry, IntegrationUnitDetail


_LLM_SAFE_REASONS = {
    "llm_unavailable": "llm_unavailable",
    "llm_timeout": "llm_timeout",
    "llm_invalid_output": "llm_invalid_output",
    "llm_privacy_declined": "llm_privacy_declined",
}


def _request_decision(gate, query, proposal, confidence, approval_reader,
                      note=None, block=None):
    """Request one human decision through the existing approval gate.

    Automated callers inject ``approval_reader``; the interactive path
    builds the request, prints the exact review block, reads one raw
    input, and decides through ``gate.decide``. EOF, timeout, or
    interruption is a rejection, never an approval.
    """
    from src.living_authenticity.knowledge.governance.approval.outcome import (
        ApprovalRequest,
    )

    if approval_reader is not None:
        trio = gate.request_approval(
            query, proposal, confidence, reader=approval_reader, note=note)
        request = trio[0]
        if not isinstance(request, ApprovalRequest):
            raise TypeError("gate must return an ApprovalRequest")
        return (request, trio[2])
    request = gate.build_request(query, proposal, confidence, note)
    if not isinstance(request, ApprovalRequest):
        raise TypeError("gate must return an ApprovalRequest")
    print(block if block is not None else gate.display(request))
    try:
        raw = input()
    except Exception:
        raw = ""
    outcome = gate.decide(request, raw)
    return (request, outcome)


def _llm_safe_reason(exc) -> str:
    """Map one provider/validation failure to the audited safe-stop reason."""
    text = str(exc) if isinstance(exc, Exception) else ""
    lowered = text.lower()
    if "llm_privacy" in lowered:
        return "llm_privacy_declined"
    if "llm_invalid_output" in lowered:
        return "llm_invalid_output"
    if "timed out" in lowered or "timeout" in lowered:
        return "llm_timeout"
    return "llm_unavailable"


def _query_text(query) -> str:
    """Best available source text of the query for human inspection."""
    for attr in ("cleaned_text", "normalized_text", "meaning",
                 "original_text"):
        value = getattr(query, attr, "")
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _safe_stop_entry(child, query, proposal, reason, unit_result=None):
    confidence = getattr(unit_result, "confidence", None) \
        if unit_result is not None else None
    filter_outcome = getattr(unit_result, "filter_outcome", None) \
        if unit_result is not None else None
    retrieval = getattr(unit_result, "retrieval_result", None) \
        if unit_result is not None else None
    comparisons = getattr(unit_result, "comparisons", ()) \
        if unit_result is not None else ()
    relation = getattr(unit_result, "relation_result", None) \
        if unit_result is not None else None
    core = getattr(unit_result, "core_analysis", None) \
        if unit_result is not None else None
    classification = getattr(unit_result, "classification", None) \
        if unit_result is not None else None
    return IntegrationUnitEntry(
        source_file=str(child), query_unit_id=query.id,
        query_source=query.source, query_position=query.position,
        proposal_hash="", proposal_action=proposal.action,
        approval_approved=False, outcome="llm_safe_stop", reason=reason,
        query_text=_query_text(query),
        proposal_title=getattr(proposal, "title", ""),
        proposal_reason=getattr(proposal, "reason", ""),
        proposed_type=getattr(proposal, "classification_type", ""),
        proposed_destination=getattr(proposal, "destination", ""),
        confidence_level=getattr(confidence, "level", "")
        if confidence is not None else "",
        confidence_basis=getattr(confidence, "basis", "")
        if confidence is not None else "",
        filter_verdict=getattr(filter_outcome, "verdict", "")
        if filter_outcome is not None else "",
        filter_basis=getattr(filter_outcome, "basis", "")
        if filter_outcome is not None else "",
        retrieval_candidates=tuple((
            {"unit_id": c.unit_id, "source": c.source,
             "position": c.position, "overlap_score": c.overlap_score,
             "excerpt": c.text[:200]}
            for c in retrieval.candidates
        )) if retrieval is not None else (),
        comparison_results=tuple((
            {"candidate_id": c.candidate_unit_id, "category": c.category,
             "shared_terms": list(c.shared_terms)}
            for c in comparisons
        )) if comparisons else (),
        relation_results=tuple((
            {"candidate_id": r.candidate_unit_id, "relation": r.relation,
             "basis": r.basis}
            for r in relation.proposals
        )) if relation is not None else (),
        core_relevance=getattr(core, "relevance", "")
        if core is not None else "",
        core_basis=getattr(core, "basis", "") if core is not None else "",
        classification_type=getattr(classification, "proposed_type", "")
        if classification is not None else "",
        classification_rationale=getattr(classification, "rationale", "")
        if classification is not None else "",
    )


def _synthesize_candidates(synthesis, unit_result, vault_context=None):
    return synthesis.synthesize(
        unit_result.query_unit, unit_result.proposal,
        unit_result.classification, unit_result.retrieval_result,
        unit_result.comparisons, unit_result.relation_result,
        unit_result.core_analysis, unit_result.confidence,
        unit_result.filter_outcome, vault_context=vault_context)


def _rendered_note_for_candidate(query, proposal, classification, confidence,
                                 candidate, generator):
    from src.living_authenticity.knowledge.output.outcome import GeneratedNote

    base = generator.generate(query, proposal=proposal,
                              classification=classification,
                              confidence=confidence)
    section = ("## LLM synthesis\n\n"
               + "Suggested type: "
               + (candidate.suggested_type or "(unresolved)") + "\n\n"
               + candidate.body.strip() + "\n\n"
               + "LLM reason: "
               + (candidate.reason or "(no reason recorded)") + "\n\n"
               + "LLM uncertainty: "
               + (candidate.uncertainty or "(none recorded)") + "\n")
    markdown = base.markdown + "\n" + section + "\n"
    return GeneratedNote(
        query_unit_id=base.query_unit_id, query_source=base.query_source,
        query_position=base.query_position, title=candidate.title,
        body=base.body + "\n\n" + section, proposed_type=base.proposed_type,
        proposal_action=base.proposal_action,
        confidence_level=base.confidence_level,
        destination=base.destination, frontmatter=base.frontmatter,
        evidence=base.evidence, uncertainties=base.uncertainties,
        provenance=base.provenance, markdown=markdown,
        strategy=base.strategy + "+llm_synthesis", note=base.note,
        is_authoritative=False, requires_human_review=True)


def _entry_display_fields(child, query, proposal, confidence,
                          unit_result, llm_result):
    """Copies of pipeline evidence for review display (informational)."""
    retrieval = unit_result.retrieval_result if unit_result else None
    comparisons = unit_result.comparisons if unit_result else ()
    relation = unit_result.relation_result if unit_result else None
    core = unit_result.core_analysis if unit_result else None
    classification = unit_result.classification if unit_result else None
    filter_outcome = unit_result.filter_outcome if unit_result else None
    return {
        "query_text": _query_text(query),
        "proposed_destination": getattr(proposal, "destination", ""),
        "llm_candidates": tuple((
            {"title": c.title, "body": c.body,
             "suggested_type": c.suggested_type,
             "reason": c.reason, "uncertainty": c.uncertainty}
            for c in llm_result.candidates
        )) if llm_result is not None else (),
        "confidence_level": confidence.level if confidence else "",
        "confidence_basis": confidence.basis if confidence else "",
        "filter_verdict": filter_outcome.verdict if filter_outcome else "",
        "filter_basis": filter_outcome.basis if filter_outcome else "",
        "retrieval_candidates": tuple((
            {"unit_id": c.unit_id, "source": c.source,
             "position": c.position,
             "overlap_score": c.overlap_score,
             "excerpt": c.text[:200]}
            for c in retrieval.candidates
        )) if retrieval is not None else (),
        "comparison_results": tuple((
            {"candidate_id": c.candidate_unit_id,
             "category": c.category,
             "shared_terms": list(c.shared_terms)}
            for c in comparisons
        )) if comparisons else (),
        "relation_results": tuple((
            {"candidate_id": r.candidate_unit_id,
             "relation": r.relation, "basis": r.basis}
            for r in relation.proposals
        )) if relation is not None else (),
        "core_relevance": core.relevance if core else "",
        "core_basis": core.basis if core else "",
        "classification_type": classification.proposed_type
        if classification else "",
        "classification_rationale": classification.rationale
        if classification else "",
    }


def _request_non_create_review(child, query, proposal, candidate, note,
                               unit_result, llm_result,
                               confidence=None, relation=None, core=None,
                               classification=None, filter_outcome=None,
                               comparisons=()):
    """Display one non-executable candidate and record an explicit review.

    Strictly review/presentation: the operator sees the rendered note and
    presses Enter to acknowledge it (EOF/timeout/interruption also
    acknowledge). No approval is requested, no decision is recorded, no
    revalidation, no execution, no audit record. This keeps the human-review
    boundary generic and interactive for every candidate instead of
    accidentally coupling the ``input()`` prompt to the ``CREATE`` branch.
    """
    from src.living_authenticity.knowledge.integration.review_display import (
        render_review_block,
    )

    block = render_review_block(
        candidate=candidate, proposal=proposal, confidence=confidence,
        filter_outcome=filter_outcome, retrieval=(
            unit_result.retrieval_result if unit_result else None),
        llm_result=llm_result, staged_text=note.markdown,
        destination=proposal.destination, proposal_hash="",
        query=query, classification=classification,
        relation=relation, core=core, comparisons=comparisons,
        non_executable=True)
    print(block)
    try:
        input()
    except Exception:
        pass


def _govern_candidate(child, query, proposal, confidence, note, gate,
                      validator, runner, pinned_staging, guarded,
                      schema_version, approval_reader, entries, audits,
                      authorized_staging="", details=None, query_unit=None,
                      unit_result=None, llm_result=None, candidate_index=0,
                      retrieval=None, relation=None, core=None,
                      comparisons=(), filter_outcome=None, classification=None):
    """Govern one candidate: approval, guard re-check, revalidation."""
    from src.living_authenticity.knowledge.governance.audit.outcome import (
        build_audit_record,
        record_audit_for_execution,
    )
    from src.living_authenticity.knowledge.integration.review_display import (
        render_review_block,
    )

    block = render_review_block(
        candidate=None, proposal=proposal, confidence=confidence,
        filter_outcome=filter_outcome, retrieval=retrieval,
        llm_result=llm_result, staged_text=note.markdown,
        destination=proposal.destination, proposal_hash="",
        query=query_unit, classification=classification,
        relation=relation, core=core, comparisons=comparisons)

    request, outcome = _request_decision(
        gate, query, proposal, confidence, approval_reader, note, block)
    live_ok, live_reason, _live = check_staging_eligible(
        pinned_staging, guarded, authorized_staging)
    display = _entry_display_fields(
        child, query, proposal, confidence, unit_result, llm_result)
    if not live_ok:
        audits.append(build_audit_record(request, outcome, None, None, proposal))
        entries.append(IntegrationUnitEntry(
            source_file=str(child), query_unit_id=query.id,
            query_source=query.source, query_position=query.position,
            proposal_hash=request.proposal_hash,
            proposal_action=proposal.action,
            approval_approved=bool(outcome.approved),
            outcome="rejected_staging_guard", reason=live_reason,
            note_markdown=note.markdown, **display,
        ))
        return
    revalidation = validator.revalidate(
        request, outcome, proposal, confidence, schema_version or "", note
    )
    if not revalidation.valid:
        audits.append(build_audit_record(
            request, outcome, revalidation, None, proposal))
        entries.append(IntegrationUnitEntry(
            source_file=str(child), query_unit_id=query.id,
            query_source=query.source, query_position=query.position,
            proposal_hash=request.proposal_hash,
            proposal_action=proposal.action,
            approval_approved=bool(outcome.approved),
            outcome="revalidation_failed",
            reason=revalidation.reason,
            note_markdown=note.markdown, **display,
        ))
        return
    execution = runner.execute(
        request, outcome, proposal, confidence, note,
        staging_root=pinned_staging,
        schema_version=schema_version or "",
    )
    audits.append(record_audit_for_execution(
        request, outcome, revalidation, execution, proposal))
    if execution.executed:
        state = "executed"
    elif (execution.failed_check == "destination"
            and "no overwrite" in (execution.reason or "")):
        state = "rejected_existing_artifact"
    else:
        state = "rejected"
    entries.append(IntegrationUnitEntry(
        source_file=str(child), query_unit_id=query.id,
        query_source=query.source, query_position=query.position,
        proposal_hash=request.proposal_hash,
        proposal_action=proposal.action,
        approval_approved=bool(outcome.approved),
        revalidation_valid=bool(revalidation.valid),
        execution_attempted=bool(execution.attempted),
        execution_permitted=bool(execution.permitted),
        execution_executed=bool(execution.executed),
        outcome=state, reason=execution.reason,
        artifact_reference=execution.artifact_path,
        note_markdown=note.markdown, **display,
    ))
    if details is not None:
        details.append(IntegrationUnitDetail(
            source_file=str(child),
            query_unit=query_unit,
            retrieval=unit_result.retrieval_result if unit_result else None,
            comparisons=unit_result.comparisons if unit_result else (),
            relation=unit_result.relation_result if unit_result else None,
            core=unit_result.core_analysis if unit_result else None,
            classification=unit_result.classification if unit_result else None,
            proposal=proposal,
            confidence=confidence,
            filter_outcome=filter_outcome,
            llm_result=llm_result,
            prompt_sha256="",
            candidate_index=candidate_index,
            human_decision=outcome,
            revalidation=revalidation,
            execution=execution,
            audit=audits[-1] if audits else None,
            note_markdown=note.markdown,
        ))

def _handle_unit(child, unit_result, gate=None, validator=None,
                 runner=None, pinned_staging="", guarded=(),
                 schema_version="", approval_reader=None, entries=None,
                 audits=None, authorized_staging="", synthesis=None,
                 generator=None, details=None, vault_context=None):
    """Govern one unit through the mandatory formal LLM stage.

    The formal stage always runs: synthesis produces 1..N candidates
    and each candidate is governed independently. When synthesis
    fails (disabled, unreachable, invalid, privacy-declined), the
    unit stops safely with an explicit reason — no candidate is
    displayed, no approval is requested, and nothing executes.

    ``vault_context`` is optional bounded excerpts passed through to
    synthesis as informational context only; it never changes proposal
    authority and never authorizes anything.
    """
    query = unit_result.query_unit
    proposal = unit_result.proposal
    confidence = unit_result.confidence
    if synthesis is None:
        raise TypeError("synthesis is required: the LLM stage is mandatory")
    if entries is None:
        raise TypeError("entries must be a list")
    if audits is None:
        raise TypeError("audits must be a list")
    if details is None:
        details = []
    if generator is None:
        from src.living_authenticity.knowledge.output.evidence_note import (
            DeterministicObsidianGenerator,
        )
        generator = DeterministicObsidianGenerator()
    try:
        llm_result, _prompt, _digest = _synthesize_candidates(
            synthesis, unit_result, vault_context=vault_context)
    except Exception as exc:
        reason = _llm_safe_reason(exc)
        entries.append(_safe_stop_entry(
            child, query, proposal, reason, unit_result=unit_result))
        return
    for candidate_index, candidate in enumerate(llm_result.candidates):
        if proposal.action != "CREATE":
            note = _rendered_note_for_candidate(
                query, proposal, unit_result.classification, confidence,
                candidate, generator)
            _request_non_create_review(
                child, query, proposal, candidate, note,
                unit_result, llm_result,
                confidence=unit_result.confidence,
                relation=unit_result.relation_result,
                core=unit_result.core_analysis,
                classification=unit_result.classification,
                filter_outcome=unit_result.filter_outcome,
                comparisons=unit_result.comparisons)
            display = _entry_display_fields(
                child, query, proposal, confidence, unit_result, llm_result)
            entries.append(IntegrationUnitEntry(
                source_file=str(child), query_unit_id=query.id,
                query_source=query.source, query_position=query.position,
                proposal_hash="", proposal_action=proposal.action,
                approval_approved=False, outcome="held_non_executable",
                reason="candidate proposal action is not CREATE",
                proposal_title=candidate.title,
                proposal_body=candidate.body,
                proposed_type=candidate.suggested_type,
                proposal_reason=candidate.reason,
                uncertainty=candidate.uncertainty,
                note_markdown=note.markdown,
                **display,
            ))
            continue
        note = _rendered_note_for_candidate(
            query, proposal, unit_result.classification, confidence,
            candidate, generator)
        _govern_candidate(
            child, query, proposal, confidence, note, gate, validator,
            runner, pinned_staging, guarded, schema_version,
            approval_reader, entries, audits, authorized_staging,
            details=details, query_unit=query, unit_result=unit_result,
            llm_result=llm_result, candidate_index=candidate_index,
            retrieval=unit_result.retrieval_result,
            relation=unit_result.relation_result,
            core=unit_result.core_analysis,
            comparisons=unit_result.comparisons,
            filter_outcome=unit_result.filter_outcome,
            classification=unit_result.classification,
        )

