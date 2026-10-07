LivingAuthenticity Environment

![Monica](Monica.jpg)
*Just Monica felt right in this place.*
[For Monica](https://hamzehhb3.substack.com/p/for-monica)

«A modular research infrastructure for organizing, retrieving, and developing knowledge with AI assistance.»

The LivingAuthenticity Environment is a long-term research infrastructure designed to support the organization, processing, retrieval, and development of personal and research knowledge.

The project is built around a simple principle:

«AI assists the research process; the researcher remains the authority.»

Architecture

- [AI Governance](AI-Governance.md) — authority boundaries, permissions, and human approval.
- [Knowledge Schema](Knowledge-Schema.yaml) — the canonical knowledge representation schema.
- [Coding Agent Access](Coding-Agent-Access.md) — filesystem and data-access boundaries.

Current Status

The project is currently focused on building its foundational infrastructure.

Implemented foundations include:

- Python project and package structure
- Centralized configuration and path management
- Text ingestion and parsing foundation
- Semantic chunking
- Dependency management
- Testing infrastructure
- Project documentation and governance
- Separation of source code from persistent data
- Deterministic analytical stages (classification, retrieval,
  comparison, relation detection, core analysis, proposal,
  confidence) plus a proposed Obsidian note representation
  (Generated Note ≠ Authoritative Note; representation does not
  execute CREATE)
- Integrated evidence-first analytical pipeline composing those
  stages in contractual order (retrieval before classification)
  with inspectable intermediate results, plus a Knowledge Filter
  as an analytical boundary signal (non-authoritative; no approval,
  authorization, or execution)
- Explicit human approval gate, fresh revalidation of the exact
  approved proposal, one-shot controlled CREATE execution into a
  staging root only, and an observational audit/traceability record
  built from those same lifecycle objects (audit authorizes and
  executes nothing)
- A mandatory formal LLM synthesis stage (`living_authenticity/llm/`)
  after the Knowledge Filter and before human review: deterministic
  bounded context, strict result contract, per-candidate governance.
  Without a usable provider the affected unit stops safely
  (`llm_safe_stop`) instead of proceeding — LLM output is never
  authorization.
- A bounded local-integration runner (`knowledge/integration/`) that
  composes those contracts over an explicitly supplied source root and
  an explicitly supplied, isolated staging root, with a staging
  eligibility guard (explicit, existing, non-symlink, outside the
  repository, outside every configured persistent-data root, pinned for
  the whole run) and per-unit human approval only — no batch, standing,
  or transferred approval
- An injectable vector-similarity retriever (`LanceDBVectorRetriever`)
  implementing the existing retrieval contract over a locally configured
  LanceDB vector store and local embedding service, wired into the
  bounded local-integration command through `--embed --vector-db`; the
  default integrated pipeline keeps its deterministic token-overlap
  retriever unless embedding is explicitly requested. Vector results are
  evidence only (Retrieval ≠ Decision; Similarity ≠ Identity), and the
  vector store is a retrieval index, never an authoritative store
- An explicit Personal Vault vs Examples operating-mode contract
  (`knowledge/modes.py`): examples mode fails closed rather than ever
  resolving the configured personal vault; mode is recorded in audit
  records
- Guarded `audit.root` configuration key with optional independently
  selectable `audit.passed` / `audit.failed` subroots, resolved-path
  validation, overlap rejection against vault/staging/vector roots
  (`check_audit_eligible`, `check_roots_disjoint`), and an append-only
  `AuditWriter` as a second, explicitly-scoped write surface disjoint
  from `ControlledExecutor` (proved by re-scoped write-surface security
  tests with negative controls). `passed/` holds ONLY the
  executed-CREATE terminal record (approved CREATE that actually
  created its staging artifact); `failed/` holds every other terminal
  record, including approved + revalidated pre-execution checkpoints
  (a safety prerequisite, never a passed outcome). Richer lifecycle
  outcomes remain on the record. Pre-execution audit persistence must
  succeed before `ControlledExecutor` may write a staging artifact
  when an audit root is configured.
- Persistent audit covering executed *and* non-executed/rejected/held
  paths: LLM safe stops, held non-executable candidates, revalidation
  failures, and rejections persist non-approved audit records with
  traceability fields (input/evidence hashes, retrieval provenance with
  considered/retrieved/passed_downstream/used_as_evidence, prompt hash,
  artifact path/hash/timestamp on success, stop reasons); credentials
  are never persisted (recursive sanitizer)
- An inspectable vault manifest/freshness contract
  (`snapshot_vault`, `diff_manifests`, `verify_index_freshness`) plus an
  explicit `synchronize_index` action — vault-derived state stays
  inspectable and synchronized only by explicit operator action, never
  by a hidden background watcher
- An LLM provider/model discovery and selection contract
  (`llm/discovery.py`): read-only local model discovery, local-first
  default, cloud as an explicit choice, validated selection states for a
  future UI; no downloads, no credential exposure, mandatory gate
  unchanged
- An artifact lifecycle matrix documenting, for every pipeline state,
  what exists in memory, what persists, and when a staging artifact does
  or does not exist ([docs/artifact-lifecycle-matrix.md](docs/artifact-lifecycle-matrix.md))
- A synthetic, public-safe example of that workflow under
  [examples/bounded_local_integration/](examples/bounded_local_integration/README.md)

Advanced components such as knowledge evolution and agent systems belong to later development phases and are not represented as completed features.

Technology

The current foundation is built primarily with Python.

LanceDB and BGE-M3 are implemented and wired as an optional local
retrieval path: embedding generation, isolated vector storage, and
vault synchronization are available through `--embed` and the vault
manifest/sync contract described above. The default integrated
pipeline keeps its deterministic token-overlap retriever unless
embedding is explicitly requested.

Configuration

Machine-specific configuration is kept outside the public repository through:

`Config/paths.local.yaml`

The committed [Config/paths.example.yaml](Config/paths.example.yaml) provides a portable template.

See [SETUP.md](SETUP.md) for development setup, configuration, dependencies, and testing.

Localization (app/staging/audit languages, English/Persian) is documented in [docs/localization.md](docs/localization.md).

Testing

Install the dev dependencies ([requirements-dev.txt](requirements-dev.txt)) and run the test suite:

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Tests use synthetic fixtures and temporary configurations only.

Direction

The long-term direction is to evolve from reliable infrastructure toward a research environment supporting:

Knowledge → Retrieval → Reasoning → Research → Knowledge Evolution

while remaining model-, provider-, and agent-independent.

Status

This project is under active development.