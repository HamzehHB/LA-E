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
| 1 | Input accepted | ingestion+units | P | yes | no | no | no | proposal preview |
| 2 | Empty/skipped input | skip counts | P run-level | yes | no | no | no | skipped: reason |
| 3 | Retrieval candidate | RetrievalResult | P hashes | yes | no | no | no | considered vs used |
| 4 | NEEDS_REVIEW | entry held | P | yes | no | no | no | review + ack |
| 5 | DO_NOT_IMPORT | entry held | P | yes | no | no | no | not imported: reason |
| 6 | CREATE rejected | entry+outcome | P | yes | NO | no | attempted only | rejected: reason |
| 7 | CREATE approved | approval+revalidation | P | yes | pending | no | pending | approved |
| 8 | LLM unavailable | safe-stop entry | P | yes llm_unavailable | no | no | no | stopped: unavailable |
| 9 | LLM malformed | safe-stop entry | P | yes llm_invalid_output | no | no | no | stopped: invalid output |
| 10 | LLM/privacy/validation failure | safe-stop entry | P | yes | no | no | no | stopped: reason |
| 11 | Revalidation failure | entry+check | P | yes | no | no | no | revalidation failed |
| 12 | Execution failure | entry+reason | P | yes | no | no | attempted+failed | write failed |
| 13 | Unsupported action | held_non_executable | P | yes | no | no | no | not executable in 0.1 |
| 14 | Successful staging execution | entry+receipt | P+S | yes linked | YES hash.md | NO | executed | staged: file + hash |
| 15 | Vault unavailable | run rejection | P run-level | yes | no | no | no | vault unreadable |
| 16 | Index unavailable/stale | staleness report | P | yes | no | no | no | index stale: reindex |
| 17 | Pre-execution audit persistence failure | in-memory only | run aborts | attempted+failed | no | no | NO | audit unavailable: stopped |
| 18 | Post-execution primary persistence failure, emergency journaling succeeds | entry+receipt | P (emergency) | yes linked (durability: confirmed) | YES hash.md | NO | executed | staged: file + hash (emergency audit) |
| 19 | Post-execution audit persistence failure (both primary & emergency fail), artifact quarantined | entry+quarantine receipt | P run-level rejected | failed (durability: failed) | QUARANTINED (.unreconciled/) | NO | executed + quarantined | rejected: audit persistence failure (quarantined) |
| 20 | Post-execution audit persistence failure, partial quarantine (source cleanup failed) | entry+quarantine receipt | P run-level rejected | failed (durability: failed) | PARTIAL (.unreconciled/ + staging/) | NO | executed + partial quarantine | rejected: audit persistence failure (partial quarantine) |

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

The UI Recent Audits journal displays operational records from `passed/` and `failed/`,
and also surfaces records from `errors/audit.jsonl` and emergency records from `errors/audit_emergency.jsonl` tagged with
`emergency: true` badges so that emergency persistence events remain fully inspectable.
