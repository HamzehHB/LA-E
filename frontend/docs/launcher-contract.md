# Launcher API Contract (proposed, non-executable)

Status: design-only for future runtime-integration review. No endpoint is implemented in the presentation-only foundation.
No mock HTTP handlers exist. Fixtures are static typed objects only.

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

- Purpose: start one bounded integration run over text input (contract only, no live run).
- Request: `{ input_text, source_mode: "personal_vault"|"examples", app_language, staging_language, audit_language }`
- Response: `{ run_id, accepted, reason }`
- Errors: validation / configuration / unavailable guards as typed kinds.
- Auth: token required. Run IDs correlate all later events/approvals/journals.
- Sync: asynchronous — returns run_id immediately; progress via `/api/events`.

## POST /api/approve

- Purpose: record one explicit human decision bound to one proposal hash.
- Request: `{ proposal_hash, query_unit_id, decision: "approve"|"reject" }`
- Response: `{ proposal_hash, approved, reason }`
- Errors: stale hash → revalidation failure kind; unknown hash → validation error.
- Auth: token required. Only explicit UI decision; never inferred from confidence/classification/filter.
- Sync: synchronous decision record; execution/revalidation remain backend-gated in the future integration layer.

## GET /api/journals

- Purpose: observe recent audit/staging records with correlation (max 10 each).
- Request: none (token header only).
- Response: `{ audits: JournalItem[], staging: JournalItem[] }` where `JournalItem = { record_id, run_id, proposal_hash, timestamp, storage_category: "passed"|"failed", outcome, artifact_reference, staging_available }`
- Errors: `connection_unavailable` when launcher is down.
- Auth: token required (vault-derived content).
- Sync: synchronous snapshot; UI refreshes explicitly (no blind polling contract yet).

## GET /api/events (SSE)

- Purpose: stream run-scoped operational events for the Current Audit view.
- Envelope: `{ kind, run_id, stage, message, timestamp }`; `kind` in `run_started|stage_started|stage_completed|status|warning|error|stopped|human_review_required|completed`.
- Content: user-safe operational info only. NEVER: private LLM reasoning, credentials, raw tracebacks, hidden deliberation.
- Auth: token required (query param or header per future integration decision).
- Correlation: every run event carries stable `run_id`; stage names use backend `STAGE_ORDER` keys.
- Sync: asynchronous stream; cancellable client-side; no timers leaked.
