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

Architecture Logic at a Glance:
- **INPUT & INGESTION:** What has been received, and how is it prepared for processing without losing meaning?
- **RETRIEVAL & COMPARISON:** What relevant evidence exists, and what similarities, differences, or contradictions does the input have with those candidates?
- **RELATION ANALYSIS & CORE ANALYSIS:** What defensible relationships exist, and how does the input relate to the central conceptual Core of the project's knowledge?
- **CLASSIFICATION, PROPOSAL, CONFIDENCE & KNOWLEDGE FILTER:** What is the nature of the input, what action is proposed, and what mathematical confidence bounds, filter rules, and uncertainties exist?
- **LLM SYNTHESIS:** How can the evidence and analyses be dynamically synthesized into a structured summary without granting decision authority to the model?
- **HUMAN REVIEW & EXPLICIT APPROVAL (UNIFIED):** What is the human inspecting, and to what exact proposal version is execution permission granted?
- **REVALIDATION & CONTROLLED EXECUTION:** Are all conditions still valid right before write, and is the authorized operation performed in a strictly controlled manner?
- **AUDIT & OUTPUT:** What happened, why did it happen, and what is the final result?

Current Status

The project is currently focused on building and operating its foundational human-governed Knowledge Management infrastructure.

Implemented foundations include:

- Python project and package structure
- Centralized configuration and path management
- Authoritative `Input` evaluation gate (`knowledge/input/processor.py`: source selection currently restricted to interactive `chat_input`, type detection currently restricted to `text` with an extensible foundation for future document/media formats, structural & substantive validation, stable `inp-<hash[:12]>` source identification, and initial safety checks)
- 8-substage `Ingestion` and parsing foundation (`reader_selection`, `reading`, `cleaning` with zero deletion of substantive content, `normalization`, `parsing`, `metadata_extraction`, `chunking`, and `extraction` with provenance)
- Semantic and structural chunking (supporting 1-to-many independent knowledge-unit extraction and lexical/semantic/future-hybrid retrieval readiness)
- Dependency management
- Testing infrastructure
- Project documentation and governance
- Separation of source code from persistent data
- Deterministic analytical stages (`retrieval`, candidate-by-candidate `comparison`, causal-comparative `relation` analysis, `core` analysis against the repository's single active `Core` note with dynamic retrieval priority weighting and archive transition for superseded Core versions, evidence-informed `classification`, `proposal` type selection [`CREATE`, `NEEDS_REVIEW`, `DO_NOT_IMPORT`], and multi-factor mathematical `confidence` calculation) plus a proposed Obsidian note representation (`Generated Note ≠ Authoritative Note`; representation does not execute `CREATE`)
- Integrated evidence-first analytical pipeline composing those
  stages in contractual order (retrieval before classification)
  with inspectable intermediate results, plus an algorithmic multi-signal `Knowledge Filter`
  as an analytical boundary signal (non-authoritative; no approval,
  authorization, or execution)
- Unified **Human Review & Explicit Approval** gate (presenting proposals and evidence for all actions to the human; explicit acknowledgment for `NEEDS_REVIEW` / `DO_NOT_IMPORT`; explicit approval, rejection, or server-side proposal editing with SHA-256 hash replacement [$H_0 \to H_1$] for `CREATE`), fresh 7-check `Revalidation` of the exact approved proposal (including single-target / no-duplicate output enforcement), one-shot `Controlled Execution` (`CREATE` into an isolated staging root only), continuous live `Current Audit` + persistent `Audit` traceability (audit authorizes and executes nothing), and structured `Output` assembly
- A mandatory formal LLM synthesis stage (`living_authenticity/llm/`)
  after the Knowledge Filter and before unified human review: deterministic
  bounded context, mandatory local model invocation via `Ollama` or privacy-gated Cloud API (`consent_required`), strict result contract, and per-candidate governance.
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
  vector store is a retrieval index, never an authoritative store.
- User-selectable retrieval strategies (`RetrievalStrategyRegistry`):
  `lexical` (deterministic token-overlap baseline; strictly zero embeddings
  and no vector allocation), `semantic` (dense vector similarity candidate
  retrieval using temporary input chunk embeddings against the persistent index;
  fails closed with typed errors rather than silent fallback when resources
  are missing), and an extensible future-ready `hybrid` contract (fails closed
  until formal fusion and scoring are implemented).
- An isolated, run-scoped temporary embedding cache (`TemporaryEmbeddingCache`):
  new-input chunks are embedded solely for active query retrieval and held in an
  in-memory cache that is deterministically cleaned up on every exit path
  (success, error, cancellation, safe-stop). Temporary input embeddings are
  strictly isolated and NEVER inserted or merged into the persistent corpus
  vector store. Cache lifecycle metrics and receipts are recorded in audit trails
  without raw vector floats or private text leakage.
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
  are never persisted (recursive sanitizer).
- Resilient audit persistence and emergency journaling (`AuditWriter.append_emergency`
  and `persist_record_resilient`): primary audit persistence performs standard
  buffered stream writes (`durability="written"`). When primary persistence fails,
  resilient persistence recovers the complete collected run history into emergency journals
  (`errors/audit_emergency.jsonl`) with hardware synchronization (`durability="confirmed"`
  via `os.fsync`), while leaving previously persisted primary records intact.
  Durable success is never falsely claimed (`durability="confirmed"` vs
  `"failed"`, `persisted=False` on failure or missing writer). File-level `fsync`
  flushes file content and metadata to storage controllers without claiming
  unconditional cross-platform power-loss crash durability (directory entry durability
  requires parent directory sync on POSIX, unsupported on Windows directory handles).
- Non-overwriting, path-confined post-execution artifact quarantine
  (`ControlledExecutor.quarantine_artifact`): if both primary and emergency
  audit persistence fail after staging execution, the artifact is moved to
  `.unreconciled/` within the staging root (falling back to sibling
  `.unreconciled.<filename>` when the `.unreconciled/` directory does not pre-exist,
  preserving the no-mkdir AST invariant). Traversal, escaping symlinks, and
  target collisions are strictly rejected without overwriting existing files.
  On POSIX hard-linking, partial cleanup failures (destination created,
  source unlink failed) are truthfully reported without rollback
  (`partial_cleanup_failed`).
- Canonical candidate identity and deterministic deduplication
  (`candidate_identity_key` with format `<source>:<position>:<unit_id>`):
  five discrete retrieval capabilities (`query_preparation`, `candidate_retrieval`,
  `relevance_assessment`, `candidate_identification`, `retrieval_evidence_recording`)
  with explicit lifecycle phases (`started`, `completed`, `failed`) and preserved
  vector-distance evidence.
- Canonical bilingual lifecycle narrative generation (English and Persian)
  and traceable translation records (`build_narrative_translation_record`),
  coordinated with the independent `Knowledge Language` domains (`app`, `staging`, `audit` in `en`/`fa`)
  so `Audit` and `Staging` outputs honor their selected domain language alongside universal on-demand `Translate`:
  strict fallback provenance attribution (`"template-fallback"` on custom
  translator failures, never falsely claiming `"custom-translator"`),
  custom notes preservation, stale detection (`is_narrative_translation_stale`),
  and explicit `persisted: false` markers for on-demand UI presentations.
- An inspectable vault manifest/freshness contract
  (`snapshot_vault`, `diff_manifests`, `verify_index_freshness`) plus
  human-gated 3-way Vault vector reconciliation (`VaultSyncSession` and `synchronize_index`:
  when authorized by the human, indexing newly added notes, replacing embeddings for modified files,
  and deleting embeddings for removed files; when declined, querying against the existing index) — vault-derived state stays
  inspectable and synchronized only by explicit operator action, never
  by a hidden background watcher.
- An LLM provider/model discovery and selection contract
  (`llm/discovery.py`): read-only local model discovery, local-first
  default, cloud as an explicit choice, and validated selection states wired into the
  launcher UI; no downloads, no credential exposure, mandatory gate
  unchanged.
- An artifact lifecycle matrix documenting, for every pipeline state,
  what exists in memory, what persists, and when a staging artifact does
  or does not exist ([docs/artifact-lifecycle-matrix.md](docs/artifact-lifecycle-matrix.md)).
- A synthetic, public-safe example of that workflow under
  [examples/bounded_local_integration/](examples/bounded_local_integration/README.md).
- A local loopback launcher (`living_authenticity/adapter/`): binds
  127.0.0.1 only, per-launch in-memory token, typed bounded errors, no
  direct filesystem writes outside authorized surfaces, and the only
  application subpath permitted to import stdlib network modules. It
  serves the React/TypeScript presentation layer (`frontend/`) together
  with a full-stack REST/SSE transport API:
  - Interactive top navigation with `TEST` / `VAULT` mode switching,
    native folder selection and validation (`staging`, `audit`, `vector_db`),
    and model configuration dropdowns (`Database` with retrieval strategy selection, `AI`, `Knowledge Language`).
  - First-time onboarding flows for initial vault vector indexing, LanceDB setup,
    and audit directory provisioning (`passed/`, `failed/`, `errors/`).
  - In-place Composer with bottom dock, substantive input boundary feedback,
    and user-selectable retrieval strategy switcher (Lexical, Semantic, disabled Hybrid).
  - 17-node chronological workflow timeline tracking live pipeline stage and substage states (with `Human Review` and `Explicit Approval` operating as a unified governance boundary).
  - Upper constellation band with 13 connected stars and DeepThink-style `Current Audit`
    overlay streaming continuous, verifiable analytical reasoning during active runs (and pausing/resuming in sync with human review).
  - Processing Result view with multi-factor numeric confidence score (`0.00`–`1.00`),
    confidence band, proposal type (`CREATE`, `NEEDS_REVIEW`, `DO_NOT_IMPORT`), unified human review actions (`Approve`,
    `Edit proposal`, `Reject`, `Accept`), actionable pause/fix/stop error recovery (with zero hallucinated cold-starts),
    and on-demand bilingual translation (`Translate`: English ↔ Persian).
  - 1:1 paired newspaper journals for Audits (passed, failed, and errors partitions) and Staging
    with 5s synchronized rotation, digests, and scrollable "View All" historical modal.
  - Run the full environment via: `python -m src.living_authenticity.adapter`.

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

Three verification states are deliberately distinct and must not be
conflated:

1. **Synthetic test coverage** — the committed suite (`python -m pytest`)
   proves contracts, governance boundaries, and security invariants using
   synthetic fixtures and temporary directories only. It reads no
   production data.
2. **Real-environment readiness** — a separate, bounded, read-only
   verification against the actual local deployment: configuration
   resolution, path-isolation guards, local model loading, and
   read-only vector-store probes. Machine-specific results stay local;
   they are never committed.
3. **An authorized real-data run** — a separately bounded operation on
   explicitly selected real data, performed only with explicit human
   authorization and every existing safety control (explicit approval,
   revalidation, controlled execution, audit). Passing tests or
   readiness checks never implies this state.

Direction

The long-term direction is to evolve from reliable infrastructure toward a research environment supporting:

Knowledge → Retrieval → Reasoning → Research → Knowledge Evolution

while remaining model-, provider-, and agent-independent.

Status

This project is under active development.