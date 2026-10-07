"""Knowledge domain: human-controlled single-input Knowledge Management.

This package groups every stage of the current backend loop:

* ``ingestion`` — readers, chunking, parsing, cleaning, metadata,
  knowledge-unit extraction (meaning-preserving, provenance-carrying);
* ``analysis`` — retrieval, comparison, relation analysis, Core
  analysis, classification (evidence-first: retrieval before
  classification; analysis never authorizes);
* ``decision`` — proposal, confidence, Knowledge Filter (analytical
  boundary signals, never approval or authorization);
* ``governance`` — explicit approval gate, fresh revalidation,
  controlled staging execution, append-only audit traceability;
* ``orchestration`` — the evidence-first pipeline composing those
  contracts in order;
* ``integration`` — the bounded local run driver composing the same
  contracts over explicit source/staging roots (no new authority);
* ``output`` — the proposed note representation (Generated Note is
  never the authoritative note);
* ``vault`` — bounded read-only vault access: discovery, context,
  manifest snapshot, freshness (never written);
* ``modes`` — Personal Vault vs Examples operating modes
  (examples fail closed, never resolve the personal vault).

What this package does NOT own: authoritative knowledge itself (the
configured vault stays read-only), execution destinations (staging
only, via ControlledExecutor), audit storage (via AuditWriter), model
files or credentials (configuration + environment only), any UI or
presentation layer.

Boundaries a researcher must hold before entering: analysis is not
authority; proposal is not approval; confidence is not permission;
retrieval is not decision-making; similarity is not identity;
a generated note is not an authoritative note; CREATE means staging
creation only, never direct vault placement.
"""
