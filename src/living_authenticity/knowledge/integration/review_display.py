"""Human-review display for one synthesis candidate.

Renders the exact block an operator sees before deciding. The candidate's
staged text is reproduced verbatim, because that text is what the
approval hash covers and what execution will write: what is displayed
and what is written must be the same object. Nothing here decides,
authorizes, or writes.
"""


def _line(label: str, value) -> str:
    text = value if isinstance(value, str) else str(value)
    return label + ": " + (text if text else "(none recorded)")


def _bullets(values, empty="(none recorded)") -> list:
    items = list(values or ())
    if not items:
        return ["- " + empty]
    return ["- " + str(item) for item in items]


def render_review_block(candidate=None, proposal=None, confidence=None,
                        filter_outcome=None, retrieval=None,
                        llm_result=None, staged_text="", destination="",
                        proposal_hash="", query=None, classification=None,
                        relation=None, core=None, comparisons=(),
                        non_executable: bool = False) -> str:
    """Render the full review block for one candidate."""
    lines = []
    lines.append("== HUMAN REVIEW: one candidate, one decision ==")
    lines.append(_line("Source", getattr(query, "source", "")))
    lines.append(_line("Title", getattr(proposal, "title", "")))
    lines.append(_line("Suggested action",
                       getattr(proposal, "action", "")))
    level = getattr(confidence, "level", "")
    lines.append(_line("Confidence",
                       level + (" (" + str(getattr(confidence, "basis", ""))
                                + ")" if level and getattr(
                                    confidence, "basis", "") else "")))
    lines.append(_line("Why", getattr(proposal, "reason", "")))
    lines.append("Evidence:")
    lines.extend(_bullets(getattr(proposal, "evidence", ())))
    lines.append("Retrieved context:")
    candidates = list(getattr(retrieval, "candidates", ()) or ())[:10]
    if candidates:
        for item in candidates:
            lines.append("- " + str(getattr(item, "unit_id", ""))
                         + " @ " + str(getattr(item, "source", ""))
                         + " (overlap " + str(getattr(item, "overlap_score", 0))
                         + ")")
    else:
        lines.append("- (no retrieval candidates recorded)")
    lines.append("Related knowledge:")
    related = [p for p in list(getattr(relation, "proposals", ()) or ())
               if getattr(p, "relation", "") == "related"]
    if related:
        for item in related:
            lines.append("- " + str(getattr(item, "candidate_unit_id", ""))
                         + " (" + str(getattr(item, "relation", "")) + ")")
    else:
        lines.append("- (none proposed)")
    lines.append("Core relevance: "
                 + (str(getattr(core, "relevance", "")) or "(unresolved)"))
    lines.append("Filter verdict: "
                 + (str(getattr(filter_outcome, "verdict", "")) or "(none)"))
    lines.append("LLM analysis:")
    if llm_result is not None and getattr(llm_result, "candidates", ()):
        for index, item in enumerate(llm_result.candidates):
            lines.append("- candidate " + str(index)
                         + " title=" + str(getattr(item, "title", ""))
                         + " suggested_type="
                         + (str(getattr(item, "suggested_type", ""))
                            or "(unresolved)")
                         + " uncertainty="
                         + str(getattr(item, "uncertainty", "")))
    else:
        lines.append("- (no synthesis result recorded)")
    lines.append("Comparison categories:")
    items = list(comparisons or ())
    if items:
        for item in items[:10]:
            lines.append("- " + str(getattr(item, "candidate_unit_id", ""))
                         + " -> " + str(getattr(item, "category", "")))
    else:
        lines.append("- (none recorded)")
    lines.append("--- Proposed note (verbatim; this is what will be "
                 "written if approved) ---")
    lines.append(staged_text if staged_text else "(empty)")
    lines.append("--- end of proposed note ---")
    lines.append(_line("Destination (inert, not written by this step)",
                       destination))
    lines.append(_line("Proposal hash", proposal_hash))
    if non_executable:
        lines.append("This proposal is not executable and requires no "
                     "approval: it is shown for human review only.")
        lines.append("Press Enter to continue to the next candidate:")
    else:
        lines.append("Approve this action? [y/N]:")
    return "\n".join(lines)


__all__ = ("render_review_block",)
