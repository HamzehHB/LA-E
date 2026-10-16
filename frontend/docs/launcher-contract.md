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

Trust: per-launch random token injected into the served page; required on
every non-health route (see folder-selection/launcher analysis). Loopback
`127.0.0.1` only.

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

- Purpose: start one bounded integration run over text input (implemented).
- Request: `{ input_text, source_mode: "personal_vault"|"examples", app_language, staging_language, audit_language }`
- Response: `{ run_id, accepted, reason }`
- Errors: validation / configuration / unavailable guards as typed kinds.
- Auth: token required. Run IDs correlate all later events/approvals/journals.
- Sync: asynchronous — returns run_id immediately; progress via `/api/events`.
- Destinations: the run prefers this launch's UI-validated session
  staging/audit selections (set via `/api/folders/pick`, memory only)
  and falls back to the config-declared locations; all values are
  revalidated by the existing destination guards before any write.

## POST /api/approve

- Purpose: record one explicit human decision bound to one proposal hash.
- Request: `{ proposal_hash, query_unit_id, decision: "approve"|"reject" }`
- Response: `{ proposal_hash, approved, reason }`
- Errors: stale hash → revalidation failure kind; unknown hash → validation error.
- Auth: token required. Only explicit UI decision; never inferred from confidence/classification/filter.
- Sync: synchronous decision record; execution/revalidation remain backend-gated in the integration layer.

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

- Purpose: live translation between English and Persian for notes, proposals, and journal excerpts. Invokes the active LLM provider if available; falls back to deterministic bilingual translation table.
- Request: `{ text: string, target_lang: "en" | "fa", source_lang?: "en" | "fa" }`.
- Response: `{ original_text: string, translated_text: string, target_lang: string, provider: string }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/folders/reveal

- Purpose: safe file listing of a configured destination directory (staging, audit, etc.) for UI inspection without executing or executing outside files.
- Request: `{ purpose: "staging" | "audit" | "vault" | "vector_db" }`.
- Response: `{ purpose: string, path: string, files: Array<{ name: string, is_dir: boolean, size: number, modified: string }> }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/vault/index

- Purpose: execute vault vector baseline indexing using BGE-M3 and LanceDB in the isolated vector store folder.
- Request: none (uses configured vault and database paths).
- Response: `{ indexed: boolean, reason?: string }`.
- Auth: token required.
- Sync: synchronous.

## POST /api/run/accept

- Purpose: explicit human review acknowledgment for non-CREATE proposals (`NEEDS_REVIEW`, `DO_NOT_IMPORT`). Acknowledges the proposal without triggering file creation.
- Request: `{ run_id: string }`.
- Response: `{ status: "acknowledged", run_id: string }`.
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
- Features: strictly excludes `errors/` directory from the journal list; computes record digests; formats human-readable titles and summaries; pairs each audit record 1:1 with its staging artifact (or marks it `staging_available: false`).
- Request: none (token header only).
- Response: `{ audits: JournalItem[], staging: JournalItem[] }` where `JournalItem = { record_id, run_id, proposal_hash, digest, title, summary, excerpt, timestamp, storage_category: "passed"|"failed", outcome, artifact_reference, staging_available }`
- Errors: `connection_unavailable` when launcher is down.
- Auth: token required.
- Sync: synchronous snapshot.

## GET /api/events (SSE)

- Purpose: stream run-scoped operational events for the Current Audit view and workflow timeline.
- Envelope: `{ kind, run_id, stage, message, timestamp, confidence_score?, confidence_score_band?, classification_type?, proposal_hash? }`.
- Kinds: `run_started|stage_started|stage_completed|status|warning|error|stopped|human_review_required|approval_decided|revalidation_result|execution_result|run_completed|run_rejected`.
- Content: user-safe operational info only with numeric confidence score (0.00–1.00) and confidence band (`high`, `moderate`, `low`). NEVER: private LLM reasoning, credentials, raw tracebacks, hidden deliberation.
- Auth: token required.
- Correlation: every run event carries stable `run_id`; stage names use backend `STAGE_ORDER` keys.
- Sync: asynchronous stream; cancellable client-side; no timers leaked.
