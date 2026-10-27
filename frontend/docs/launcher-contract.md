# Launcher API Contract (implemented)

Status: implemented by `src/living_authenticity/adapter/`
(loopback-only `server.py` + application core `app.py`) and the
React client `frontend/src/live/client.ts`. No mock HTTP handlers
exist. Fixtures are static typed objects used only when no
launcher token is available (fixture/demo mode).

Trust: per-launch random token injected into the served page as
`window.__LAE_BOOTSTRAP__`; required on every non-health route.
Loopback `127.0.0.1` only. Delivery: `python -m
src.living_authenticity.adapter` serves the built `frontend/dist`
bundle and opens the system browser; without `dist`, a
placeholder page explains the frontend must be built first.
Without a launcher token (plain `vite dev`), the UI stays in
fixture/demo mode and performs no backend calls.

## GET /api/health

- Purpose: instance discovery; detect already-running launcher; reject foreign occupant.
- Request: none.
- Response: `{ app: "living-authenticity", version: string, instance_id: string }`
- Errors: connection refused → `connection_unavailable`.
- Auth: none (read-only marker).
- Sync: synchronous.

## POST /api/folders/pick

- Purpose: open the OS native folder dialog server-side for one purpose enum.
- Request: `{ purpose: "vault"|"staging"|"audit"|"vector_db"|"local_llm"|"embedding_model" }` (no path parameter).
- Response: `{ purpose, status: "configured"|"invalid"|"unavailable"|"cancelled", display, reason, validated_at }`
- Errors: `{ kind: "unavailable_resource"|"user_cancellation"|"validation_error", messageKey }`
- Auth: launch token required (mutating-adjacent: opens OS dialog + may store selection).
- Sync: synchronous from the UI perspective; server runs dialog on a dedicated thread; concurrent picks → `unavailable/picker busy`.
- Validation: purpose-specific backend guard (PathBoundary / check_staging_eligible / check_audit_eligible / check_vector_store_eligible / model discovery). Frontend proposals never confer validity.

## POST /api/run

- Purpose: start one bounded integration run over interactive chat text input (`chat_input` active in current scope; extensible foundation for future document/media formats) across the 17-step (16 unified-stage) workflow.
- Request: `{ input_text, source_mode: "personal_vault"|"examples", retrieval_mode?: "lexical"|"semantic", app_language?: "en"|"fa", staging_language?: "en"|"fa", audit_language?: "en"|"fa", source_type?: "chat_input", input_id?: string }`
- Response (HTTP 202): `{ run_id, accepted, reason, retrieval_mode, app_language, staging_language, audit_language, source_type }`
- Input Evaluation Boundary: `process_input` validates the 5 Input sub-stages (source selection, type detection, structural bounds + $\ge 50$-letter substance threshold `DEFAULT_MIN_LETTERS = 50`, source identification `inp-<hash[:12]>`, and safety checks) before scheduling downstream ingestion.
- Errors: validation / configuration / unavailable guards as typed kinds.
- Auth: token required. Run IDs correlate all later events/approvals/journals.
- Sync: asynchronous — returns `run_id` immediately; live DeepThink-style progress streamed via `/api/events`.
- Destinations: the run prefers this launch's UI-validated session
  staging/audit selections (set via `/api/folders/pick`, memory only)
  and falls back to the config-declared locations; all values are
  revalidated by the existing destination guards before any write.

## POST /api/approve

- Purpose: record one explicit human decision (`approve` or `reject`) bound to one `proposal_hash` in the unified **Human Review & Explicit Approval** stage.
- Request: `{ proposal_hash, query_unit_id, decision: "approve"|"reject" }`
- Response: `{ proposal_hash, approved, reason }`
- Eligibility & Errors:
  - Only `CREATE` proposals may be approved for execution; attempting to `approve` a non-CREATE proposal (`NEEDS_REVIEW`, `DO_NOT_IMPORT`) returns HTTP 400 `validation_error` (non-CREATE proposals are acknowledged via `POST /api/run/accept` or rejected).
  - Stale/unknown hash or mismatched `query_unit_id` → HTTP 400 `validation_error`.
  - Cancelled, timed-out, or already-answered approval → HTTP 409 `conflict`.
- Auth: token required. Only explicit UI decision; never inferred from confidence/classification/filter.
- Sync: synchronous decision record; 7-check revalidation and controlled execution remain backend-gated in the integration layer.

## POST /api/proposal/edit

- Purpose: server-side edit of the pending CREATE proposal. The backend
  resolves the authoritative proposal/note from the active pending
  approval (never from client state), validates the edit, recomputes the
  authoritative content hash (a client-supplied hash is ignored), and
  supersedes the pending approval so the edited version requires a fresh
  explicit approval. Non-CREATE actions are rejected.
- Request: `{ title?, body?, reason?, proposal_hash? }` (hash accepted for
  interface compatibility only; non-authoritative).
- Response: `{ proposal_hash, supersedes, query_unit_id, proposal_action, title, body, confidence_level, review_state, reason }`.
- Errors: validation errors for no pending proposal, non-eligible action,
  or empty/oversize content.
- Auth: token required.

## POST /api/cancel

- Purpose: cooperative, run-scoped cancellation. The active run observes
  the request at the next safe boundary (between units, or immediately
  before the controlled write); no write occurs after cancellation.
- Request: `{ run_id }`.
- Response: `{ run_id, status: "cancel_requested"|"already_completed", reason }`.
- Errors: validation errors for unknown/inactive run.
- Auth: token required.

## GET /api/state

- Purpose: snapshot all backend configurations, folder validity states, active LLM provider/model, and any paused runs on initial page load or reconnect.
- Request: none (token header required).
- Response: `{ app, version, instance_id, source_mode, retrieval_mode, app_language, staging_language, audit_language, folders: { vault, staging, audit, vector_db, local_llm, embedding_model }, llm: { provider, active_model, available_models }, paused_run: { run_id, stage, reason, input_text } | null }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/config

- Purpose: update and persist application settings (paths, LLM parameters, localization) into local YAML configuration files.
- Request: `{ vault_path?, staging_path?, audit_path?, vector_db_path?, local_llm_path?, embedding_model_path?, llm_provider?, local_model?, temperature?, context_window?, app_language?, staging_language?, audit_language? }`.
- Response: `{ status: "ok", updated: string[] }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/translate

- Purpose: live translation between English and Persian for notes, proposals, dynamic text, and audit lifecycle narratives. Invokes the active LLM provider if available; falls back to deterministic bilingual template localization.
- Request: `{ text: string, target_lang: "en" | "fa", source_lang?: "en" | "fa", source_kind?: string, source_id?: string }`.
- Response: Returns a structured, traceable translation object:
  ```json
  {
    "record_id": "tr-...",
    "source_id": "...",
    "source_kind": "audit_narrative" | "dynamic_text",
    "source_language": "en" | "fa",
    "target_language": "en" | "fa",
    "source_hash": "<sha256-hex>",
    "source_text": "...",
    "translated_text": "...",
    "translator": "ollama:..." | "template-fallback",
    "lifecycle_state": "current" | "stale" | "unavailable",
    "persisted": false,
    "reason": "...",
    "created_at": "ISO-8601",
    "original_text": "...",
    "target_lang": "...",
    "provider": "..."
  }
  ```
- Durability: Explicitly marked `persisted: false` because on-demand UI translations are presentation-layer only and do not write to persistent audit logs.
- Provenance: When a custom translator fails or returns empty/whitespace text, `translator` is truthfully attributed to `"template-fallback"`, never `"custom-translator"`.
- Auth: token required.
- Sync: synchronous.

## GET /api/vault/sync/status

- Purpose: inspect current Vault persistent vector index authorization and freshness status without mutating the index.
- Request: none (token header required).
- Response: `{ authorized: boolean, status: "unauthorized"|"authorizing"|"sync_in_progress"|"fresh"|"stale"|"change_detected"|"revoked"|"error", vault_id: string, vault_root: string, vector_db_path: string, stored_vectors: number, diff?: { added: string[], modified: string[], removed: string[], stale: boolean } }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/vault/sync/authorize

- Purpose: explicitly authorize persistent vector indexing for the configured Vault.
- Request: `{ vault_root?: string }`.
- Response: `{ authorized: boolean, status: string, vault_id: string, vault_root: string, vector_db_path: string, ... }`.
- Auth: token required.
- Sync: synchronous authorization; executes initial sync.

## POST /api/vault/sync/reconcile

- Purpose: explicitly reconcile and re-synchronize added, modified, or removed notes in the dedicated Vault vector store (`vault_<sha256[:12]>`):
  1. **Added Notes:** Generates embeddings for newly added Vault `.md` files and stores them in the isolated database.
  2. **Modified Notes:** Generates fresh embeddings for modified files and replaces their outdated vectors in the database.
  3. **Deleted Notes:** Removes vectors corresponding to files deleted from the Vault.
- Request: `{}` (uses active authorized Vault session).
- Response: `{ status: "fresh" | "stale", added_count: number, modified_count: number, removed_count: number, stored_vectors: number }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/vault/sync/decide

- Purpose: record the explicit human operator decision (`approve` or `reject`) when pre-retrieval Vault changes are detected during a semantic run (`sync_approval_required` SSE event).
  - If `approve`: performs 3-way Vault reconciliation (indexing added notes, replacing modified file embeddings, and deleting removed file embeddings) before executing candidate retrieval against the updated database.
  - If `reject`: leaves the persistent vector database untouched, preserves its stale status truthfully, and proceeds with candidate retrieval against the existing database using the run's temporary input embeddings (`TemporaryEmbeddingCache`).
- Request: `{ run_id: string, vault_id: string, decision: "approve" | "reject" }`.
- Response: `{ status: "ok", run_id: string, vault_id: string, decision: string }`.
- Errors: HTTP 400 `validation_error` (missing fields or invalid decision); HTTP 404 `not_found` (no pending sync approval for `run_id`); HTTP 422 `unprocessable_entity` (`vault_id` mismatch); HTTP 409 `conflict` (already decided/cancelled).
- Auth: token required.
- Sync: synchronous.

## POST /api/vault/sync/revoke

- Purpose: explicitly revoke authorization for Vault synchronization and stop all index sync actions.
- Request: `{}`.
- Response: `{ authorized: false, status: "revoked", vault_id: string }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/folders/reveal

- Purpose: safe file listing of a configured destination directory (staging, audit, etc.) for UI inspection without executing or opening outside files.
- Request: `{ purpose: "staging" | "audit" | "vault" | "vector_db" }`.
- Response: `{ purpose: string, path: string, files: Array<{ name: string, is_dir: boolean, size: number, modified: string }> }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/vault/index

- Purpose: execute vault vector baseline indexing using BGE-M3 and LanceDB in the isolated vector store folder.
- Request: `{}` (uses configured vault and database paths).
- Response: `{ indexed: boolean, reason?: string }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/consent

- Purpose: answer one pending per-call cloud privacy consent request (`consent_required` SSE event) bound to the exact prompt digest (`consent_id`) before dispatching a non-local LLM synthesis request.
- Request: `{ consent_id: string, decision: "approve" | "reject" }`.
- Response: `{ consent_id: string, approved: boolean }`.
- Errors: HTTP 409/400 `validation_error` on unknown or already-answered `consent_id`.
- Auth: token required.
- Sync: synchronous.

## POST /api/run/accept

- Purpose: explicit human review acknowledgment in the unified **Human Review & Explicit Approval** stage for non-CREATE proposals (`NEEDS_REVIEW`, `DO_NOT_IMPORT`). Acknowledges human inspection of the proposal and resumes the pipeline to terminal Audit/Output without triggering file creation.
- Request: `{ proposal_hash: string, run_id?: string }` (`proposal_hash` must be a valid 64-character hex SHA-256 digest matching the active pending review).
- Response: `{ accepted: true, action: string, run_id: string, proposal_hash: string }`.
- Errors:
  - HTTP 400 `validation_error`: missing, non-64-hex, or mismatched `proposal_hash`, or if the pending proposal is `CREATE` (`CREATE` proposals require `/api/approve`).
  - HTTP 409 `conflict`: run already completed, cancelled, timed out, already answered, or `run_id` mismatch.
- Auth: token required.
- Sync: synchronous.

## POST /api/run/fix

- Purpose: error recovery / paused run resumption. Submits corrected input or parameters for a paused/safe-stopped run.
- Request: `{ run_id: string, input_text?: string, source_mode?: string, retrieval_mode?: string }`.
- Response: `{ status: "resumed", run_id: string }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/run/stop

- Purpose: cleanly terminates a paused or safe-stopped run without performing writes.
- Request: `{ run_id: string }`.
- Response: `{ status: "stopped", run_id: string }`.
- Auth: token required.
- Sync: synchronous.

## GET /api/journals

- Purpose: observe recent audit/staging records with correlation (up to 10 records).
- Features: reads operational records from `passed/audit.jsonl`, `failed/audit.jsonl`, and `errors/audit.jsonl`, plus emergency audit records from `errors/audit_emergency.jsonl` (with `emergency: true`); computes record digests; formats human-readable titles and summaries; pairs each audit record 1:1 with its staging artifact (or marks it `staging_available: false`).
- Durability and Emergency Recovery Semantics:
  - Primary audit persistence uses standard buffered file stream writes (`durability="written"`).
  - Emergency audit writes enforce `handle.flush()` and `os.fsync(handle.fileno())` (`durability="confirmed"`).
  - When primary persistence fails, resilient persistence recovers the complete collected audit history for the affected run up to the point of failure into `errors/audit_emergency.jsonl`, while retaining all previously persisted primary records in `passed/` or `failed/` intact.
  - Complete Recovery Verification: Resilient persistence validates the recovery outcome for all required records across the run. If any required record fails to write, has a malformed/unconfirmed receipt, has an identity mismatch, or experiences a content conflict, the recovery fails closed (`persisted=False`, `durability="failed"`, `recovery_all_ok=False`), and quarantine is triggered for unpersisted staging artifacts.
  - Integrity-Validating Idempotency & Conflict Handling: Repeated recovery attempts validate stored domain content against in-memory records. Identical records are deduplicated without re-appending, while content conflicts are recorded as explicit conflicts and preserved as supplemental conflict records without overwriting historical entries.
  - Crash durability limits: File-level `fsync` flushes file content and metadata to the storage controller, but does not provide an unconditional cross-platform guarantee of physical-media persistence across sudden hardware loss or power failure (POSIX directory entry durability requires syncing parent directory, unsupported on Windows directory handles).
- Staging and Quarantine: Items that experienced post-execution audit failure reflect quarantine receipts (`quarantined: true`, `quarantine_status: "quarantined" | "partial_cleanup_failed"`, `quarantine_path: string`).
- Request: none (token header only).
- Response: `{ audits: JournalItem[], staging: JournalItem[] }` where `JournalItem = { record_id, run_id, proposal_hash, digest, title, summary, excerpt, timestamp, storage_category: "passed"|"failed"|"errors", outcome, artifact_reference, staging_available, emergency?, quarantined?, quarantine_status? }`
- Errors: `connection_unavailable` when launcher is down.
- Auth: token required.
- Sync: synchronous snapshot.

## GET /api/events (SSE)

- Purpose: stream run-scoped operational events for the Current Audit view and workflow timeline.
- Envelope: `{ kind, run_id, stage, substage?, operation?, phase?, message, timestamp, confidence_score?, confidence_score_band?, classification_type?, proposal_hash?, candidate_count?, unit_count? }`.
- Kinds: `run_started|stage_started|stage_completed|status|warning|error|stopped|human_review_required|approval_decided|consent_required|sync_approval_required|revalidation_result|execution_result|run_completed|run_rejected`.
- Stages, Sub-Stages, and Live `Current Audit` Continuity:
  - Streams continuous, verifiable DeepThink-style analytical progress across all 17 stages (16 unified operational stages), respecting the selected `Audit` and `Staging` Knowledge Languages (`en`/`fa`).
  - `ingestion`: 8 discrete substages (`reader_selection`, `reading`, `cleaning`, `normalization`, `parsing`, `chunking`, `metadata_extraction`, `extraction`).
  - `retrieval`: discrete operations (`query_preparation`, `candidate_retrieval`, `relevance_assessment`, `candidate_identification`, `retrieval_evidence_recording`) with explicit phases `started`, `completed`, and `failed`, plus ephemeral `TemporaryEmbeddingCache` cleanup receipts and human-gated Vault sync events (`sync_approval_required`).
  - `human_review` (Unified Human Review & Explicit Approval): emits `human_review_required` for all proposal actions (`CREATE`, `NEEDS_REVIEW`, `DO_NOT_IMPORT`), pausing the live `Current Audit` stream in sync with human inspection/editing and resuming continuously upon `approval_decided`.
- Rejection and Quarantine: When audit persistence fails post-execution, terminal rejection events explicitly report `"audit persistence failure: artifact quarantined"` or `"audit persistence failure: partial quarantine"`.
- Client Correlation, Filtering, and Substep Lifecycle Contract:
  - Clients MUST strictly filter incoming SSE events by the active `run_id` (`filterEventsByRun(events, run.run_id)`). When `run_id` is omitted, events must NEVER be selected solely because they occur later in the array.
  - Events are scoped to a run only if there is a single distinct run, or if all distinct runs possess valid parseable timestamps that unambiguously establish a strictly latest run. If timestamps are missing, mixed, unparseable, or tied, correlation remains uncertain and returns an empty event list (`[]`) to prevent cross-run pollution or invented substep states.
  - Substep lifecycle states are strictly `"pending" | "current" | "completed" | "failed" | "cancelled" | "stopped" | "unknown"`.
  - Substep completion MUST NOT be inferred merely from relative array index order; substeps are marked completed only upon receiving an explicit completion signal (`phase === "completed"`), never inferred from overall stage completion or order alone.
  - Substeps MUST NEVER convert a failed or cancelled operation into `pending` merely because it is no longer active. When retrieval stops or completes, failed and completed substeps remain visibly distinguished in their truthful terminal states.
- Content: user-safe operational info only with numeric confidence score (0.00–1.00) and confidence band (`high`, `moderate`, `low`). NEVER: private LLM reasoning, credentials, raw tracebacks, hidden deliberation.
- Auth: token required.
- Correlation: every run event carries stable `run_id`; stage names use backend `STAGE_ORDER` keys.
- Sync: asynchronous stream; cancellable client-side; no timers leaked.
