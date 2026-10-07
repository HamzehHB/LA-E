"""LivingAuthenticity backend: human-governed Knowledge Management.

Living Authenticity is a research environment for organizing,
retrieving, and developing personal knowledge with AI assistance,
built on one principle: AI analyzes and proposes; the researcher
remains the authority.

The current backend implements the smallest reliable single-input
loop: ingestion (readers, chunking, parsing, cleaning,
metadata/provenance, unit extraction) -> evidence-first analysis
(retrieval, comparison, relation analysis, Core analysis,
classification) -> proposal, confidence, Knowledge Filter (analytical
signals only) -> mandatory LLM synthesis (bounded, provider-replaceable,
failure stops the unit safely) -> human review -> explicit approval ->
fresh revalidation -> controlled CREATE execution into an isolated
staging root only -> append-only audit traceability (passed ONLY when
the approved CREATE actually created its artifact).

Major members: ``knowledge`` (the domain loop above), ``llm``
(replaceable synthesis capability, never authority), ``security``
(default-deny path boundary + secret detection), ``database`` /
``embedding`` (derived retrieval index only, never authoritative
knowledge), ``cli`` (thin operator client of the backend).

Analysis never writes authoritative knowledge; the configured vault
stays read-only; staging, audit, and index are three disjoint write
surfaces; Personal Vault vs Examples modes fail closed; Core is
schema-derived approved reference, analytical only. Deliberately out
of scope: UI, localization/presentation, UPDATE/MERGE/ARCHIVE,
autonomous evolution.
"""
