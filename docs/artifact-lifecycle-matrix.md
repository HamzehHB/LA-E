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
| 17 | Audit persistence failure | in-memory only | run aborts | attempted+failed | no | no | NO | audit unavailable: stopped |

Rules: rejected/held paths persist under the `failed` storage category
with `artifact: none`; ONLY the successful CREATE terminal record (an
approved CREATE that actually created its staging artifact) persists
under `passed` and links `artifact_path` / `artifact_sha256` /
`artifact_created_at` plus `proposal_hash` and `run_id`/`record_id`.
Approved + revalidated pre-execution checkpoints are a safety
prerequisite persisted BEFORE execution and always belong under
`failed` — approval and revalidation are not successful execution. If
execution fails, the terminal outcome is failed with no staging
artifact claimed. Audit never holds credentials.
`passed`/`failed` are storage partitions, not a flattened boolean
success field — richer lifecycle outcomes remain on the record.
When `audit.root` is configured, pre-execution audit persistence must
succeed before ControlledExecutor may create a staging artifact
(row 17).
