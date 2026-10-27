# Artifact Lifecycle Matrix — LivingAuthenticity Environment

Status: normative for 0.1 backend foundations.
Authoritative Vault content is never written by the pipeline.
Staging artifacts are controlled CREATE outputs only.
Audit records are traceability only.

M = memory, P = persisted audit record, S = staging artifact,
V = authoritative vault write (always NO in 0.1).
Correlation: run_id / record_id chain.

| # | State | Memory | Persisted | Audit | Staging | Vault | Exec | UI shows |
|---|-------|--------|-----------|-------|---------|-------|------|----------|
| 1 | Input accepted ($\ge 50$ letters, valid `chat_input` text) | provenance+ingestion+units | P | yes | no | no | no | live DeepThink trace + proposal preview |
| 2 | Empty/skipped/rejected or $<50$-letter (`NEEDS_CLARIFICATION`) input | evaluation+skip counts (no raw text persisted) | P run-level (`failed/`) | yes | no | no | no | rejected/clarification + `Fix / Retry / Stop` |
| 3 | Retrieval candidate & ephemeral input embeddings | `RetrievalResult` + `TemporaryEmbeddingCache` (wiped at run end) | P hashes & cache receipt | yes | no | no | no | considered vs used + substep pills |
| 4 | `NEEDS_REVIEW` (Unified Human Review) | entry held + review request | P (`failed/`) | yes | no | no | no | full proposal review card + `Acknowledge` / `Reject` |
| 5 | `DO_NOT_IMPORT` (Unified Human Review) | entry held + review request | P (`failed/`) | yes | no | no | no | full proposal review card (not imported: reason) + `Acknowledge` / `Reject` |
| 6 | `CREATE` rejected (Unified Human Review & Approval) | entry+outcome | P (`failed/`) | yes | NO | no | attempted only | rejected: reason |
| 7 | `CREATE` edited ($H_0 \to H_1$) & approved (Unified Human Review & Approval) | superseded $H_0$ + approval($H_1$)+7-check revalidation | P (pre-exec checkpoint in `failed/`) | yes | pending | no | pending | edited/approved ($H_1$) |
| 8 | LLM unavailable | safe-stop entry | P | yes `llm_unavailable` | no | no | no | stopped: unavailable + `Fix / Retry / Stop` |
| 9 | LLM malformed | safe-stop entry | P | yes `llm_invalid_output` | no | no | no | stopped: invalid output + `Fix / Retry / Stop` |
| 10 | LLM/privacy/validation failure | safe-stop entry | P | yes | no | no | no | stopped: reason + `Fix / Retry / Stop` |
| 11 | Revalidation failure (any of 7 checks, incl. duplicate-target prevention) | entry+check | P (`failed/`) | yes | no | no | no | revalidation failed |
| 12 | Execution failure | entry+reason | P (`failed/`) | yes | no | no | attempted+failed | write failed |
| 13 | Unsupported action (`UPDATE`/`MERGE`/`ARCHIVE` mapped to review) | held_non_executable | P (`failed/`) | yes | no | no | no | not executable in 0.1 (review + ack) |
| 14 | Successful staging execution | entry+receipt | P+S (`passed/`) | yes linked | YES `<proposal_hash>.md` | NO | executed | staged: file + hash |
| 15 | Vault unavailable | run rejection | P run-level | yes | no | no | no | vault unreadable + recovery |
| 16 | Index unavailable/stale (Human-Gated 3-Way Vault Sync) | staleness/diff report (`added`/`modified`/`removed`) | P (+ isolated vector store update ONLY if human authorizes) | yes | no | no | no | sync prompt (approve: reconcile DB; reject: query existing DB) |
| 17 | Pre-execution audit persistence failure | in-memory only | run aborts | attempted+failed | no | no | NO | audit unavailable: stopped |
| 18 | Post-execution primary persistence failure, emergency journaling succeeds | entry+receipt | P (emergency) | yes linked (`durability: confirmed`) | YES `<proposal_hash>.md` | NO | executed | staged: file + hash (emergency audit) |
| 19 | Post-execution audit persistence failure (both primary & emergency fail), artifact quarantined | entry+quarantine receipt | P run-level rejected | failed (`durability: failed`) | QUARANTINED (`.unreconciled/`) | NO | executed + quarantined | rejected: audit persistence failure (quarantined) |
| 20 | Post-execution audit persistence failure, partial quarantine (source cleanup failed) | entry+quarantine receipt | P run-level rejected | failed (`durability: failed`) | PARTIAL (`.unreconciled/` + `staging/`) | NO | executed + partial quarantine | rejected: audit persistence failure (partial quarantine) |
| 21 | On-demand UI translation (`POST /api/translate`) | `TranslationRecord` (`persisted: false`, `source_hash` bound) | NO (memory/UI only) | no | no | no | no | translated view (`en` $\leftrightarrow$ `fa`) + `[stale]` badge on edit |

Rules: rejected/held paths persist under the `failed` storage category
with `artifact: none`; unhandled runtime execution faults persist under `errors`;
ONLY the successful CREATE terminal record (an approved CREATE that actually created its
staging artifact and was confirmed written to audit storage) persists under `passed` and links `artifact_path` /
`artifact_sha256` / `artifact_created_at` plus `proposal_hash` and `run_id`/`record_id`.
Approved + revalidated pre-execution checkpoints are a safety
prerequisite persisted BEFORE execution and always belong under
`failed` — approval and revalidation are not successful execution. If
execution fails, the terminal outcome is failed with no staging
artifact claimed. Audit never holds credentials.
`passed`/`failed`/`errors` are storage partitions, not a flattened boolean
success field — richer lifecycle outcomes remain on the record.
When `audit.root` is configured, pre-execution audit persistence must
succeed before ControlledExecutor may create a staging artifact
(row 17).

Post-Execution Audit Durability and Quarantine Rules:
1. Resilient Audit Persistence & Recoverable History:
   - When ControlledExecutor writes a staging artifact, post-execution audit persistence is attempted immediately.
   - Primary persistence writes to `passed/audit.jsonl` using buffered file stream writes (`durability="written"`), confirming the operating system accepted the write without hardware-level disk synchronization (`fsync`).
   - If primary write fails, resilient persistence recovers the complete collected audit history for the affected run up to the point of failure into `errors/audit_emergency.jsonl`, while retaining all previously persisted primary records in `passed/` or `failed/` intact.
   - Emergency writes enforce `handle.flush()` and `os.fsync(handle.fileno())` to synchronize file data and inode metadata to the storage controller (`durability="confirmed"`).
   - Complete Recovery Verification: `persist_record_resilient` validates the recovery outcome across all required records in the affected run via `EmergencyRecoveryResult`. If any required record fails to be written, returns an unconfirmed receipt, has a record_id mismatch, or experiences a content conflict, the recovery fails closed (`persisted=False`, `durability="failed"`, `recovery_all_ok=False`), and quarantine is triggered for unpersisted staging artifacts.
   - Integrity-Validating Idempotency & Conflict Preservation: repeated recovery attempts validate stored domain attributes against in-memory records. If identical, the existing entry is reused with its verified digest. If a record with the same ID has conflicting domain attributes, an explicit conflict is recorded (`conflict=True`, `persisted=False`), and a supplemental conflict entry is appended without overwriting or deleting historical journal entries. Malformed entries are conservatively segregated.
   - If emergency persistence succeeds with confirmed `fsync` across all required records, the receipt reports `persisted=True`, `mode="emergency"`, `durability="confirmed"`, and the artifact remains validly staged (row 18).
   - Durability limits: File-level `fsync` flushes file content and metadata to the storage controller, but does not provide an unconditional cross-platform guarantee of physical-media persistence across sudden hardware loss or power failure. On POSIX systems, directory entry durability requires an additional `fsync` on the parent directory descriptor, which is not supported on Windows directory handles.
   - All receipts are verified fail-closed via `validate_persistence_receipt`: invalid, contradictory, or unconfirmed durability receipts fail closed (`persisted=False`).
2. Failure to Persist & Non-Overwriting Quarantine:
   - If BOTH primary persistence and emergency journaling fail (or `os.fsync` raises an error):
     - Durable success is NEVER reported (`persisted=False`, `durability="failed"`). A missing or `None` AuditWriter or an unconfirmed durability receipt likewise reports `persisted=False`.
     - The staged artifact is immediately quarantined via `quarantine_artifact` to prevent unpersisted/untracked artifacts from lingering in staging.
     - Destination selection policy:
       - If `.unreconciled/` directory exists in the staging root, the target is `.unreconciled/{filename}`.
       - If `.unreconciled/` directory is absent, quarantine falls back to the sibling file `.unreconciled.{filename}` in the staging root, strictly preserving the AST invariant that ControlledExecutor and quarantine never create unverified directories (`no-mkdir`).
     - Non-overwriting guarantee: if the target destination already exists, quarantine refuses to overwrite it and reports a collision error (`FileExistsError`), preserving both files intact.
     - Path-confinement: source and destination must resolve strictly inside the authorized staging root. Absolute external paths, traversal components (`..`), and escaping symlinks are rejected.
     - Full vs. Partial Quarantine: on POSIX systems where hard-linking is followed by `os.unlink(source)`, if unlinking fails after the destination link is created, `quarantine_artifact` returns `status="partial_cleanup_failed"` without destructive rollback. Both copies remain inspectable on disk, and the runner truthfully records `"audit persistence failure: partial quarantine"` (row 20). When source removal succeeds, status is `"quarantined"` and the runner records `"audit persistence failure: artifact quarantined"` (row 19).

Continuous Live `Current Audit`, Unified Review Synchronization, and Knowledge Language Rules:
- **Live DeepThink-Style `Current Audit` Continuity:** Across Stages 1–17, the backend streams live, verifiable analytical reasoning steps to `Current Audit` in memory (`M`). During the unified **Human Review & Explicit Approval** stage (rows 4–7), `Current Audit` pauses in sync with the human review state (`human_review_required`) and resumes continuously once the human decision (`approval_decided`) is recorded.
- **Knowledge Language Authoring & Ephemeral `Translate`:** Persistent `Audit` narratives (`P`) and `Staging` artifacts (`S`) are written and recorded directly in their selected domain languages (`language.audit` and `language.staging` in `en` or `fa`). On-demand translations (`POST /api/translate`, row 21) exist strictly in memory (`persisted: false`) with SHA-256 `source_hash` binding and never mutate `P`, `S`, or `V`.

The UI Recent Audits journal displays operational records from `passed/` and `failed/`,
and also surfaces records from `errors/audit.jsonl` and emergency records from `errors/audit_emergency.jsonl` tagged with
`emergency: true` badges so that emergency persistence events remain fully inspectable.
