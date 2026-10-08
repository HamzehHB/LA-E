/**
 * Synthetic presentation-only fixtures.
 * No backend execution, no filesystem, no real paths. Display uses
 * <TEST_*> placeholders. Proposal hashes are static display strings —
 * NOT computed by the authoritative algorithm.
 */
import type { JournalRow, CurrentRunView } from "../contracts/ui";

export const SYNTH_VAULT = "<TEST_VAULT>";
export const SYNTH_STAGING = "<TEST_STAGING>";
export const SYNTH_AUDIT = "<TEST_AUDIT>";

export const FIXTURE_RUN: CurrentRunView = {
  run_id: "run-fixture-001",
  current_stage: "proposal",
  status: "fixture awaiting mock review",
  proposal_action: "CREATE",
  proposal_title: "Fixture proposal: organized reading note",
  proposal_body:
    "This is synthetic fixture text. It demonstrates the Processing Result layout only and authorizes nothing.",
  confidence_level: "weak",
  filter_verdict: "pass_to_review",
  proposal_hash: "fixture-hash-display-only-0001",
  review_state: "awaiting_review",
  edited_body: null,
  error: null,
  stopped: false,
  stop_reason: "",
};

export const FIXTURE_RUN_NEEDS_REVIEW: CurrentRunView = {
  ...FIXTURE_RUN,
  run_id: "run-fixture-002",
  current_stage: "knowledge_filter",
  proposal_action: "NEEDS_REVIEW",
  proposal_title: "Fixture proposal: needs review",
  filter_verdict: "hold_for_review",
  proposal_hash: "fixture-hash-display-only-0002",
};

export const FIXTURE_RUN_DO_NOT_IMPORT: CurrentRunView = {
  ...FIXTURE_RUN,
  run_id: "run-fixture-003",
  current_stage: "knowledge_filter",
  proposal_action: "DO_NOT_IMPORT",
  proposal_title: "Fixture proposal: do not import",
  filter_verdict: "do_not_import",
  proposal_hash: "fixture-hash-display-only-0003",
};

function journalRow(id: string, statusKey: string, stagingNoteKey: string | null): JournalRow {
  return {
    id,
    timestamp: "2026-10-08T12:00:00",
    statusKey,
    stagingNoteKey,
  };
}

/** Latest-10 audit fixture rows covering passed/failed + missing-staging cases. */
export const FIXTURE_AUDITS: JournalRow[] = [
  journalRow("rec-fixture-010", "journal.passed", "journal.stagingAvailable"),
  journalRow("rec-fixture-009", "journal.failed", "journal.stagingNotCreated"),
  journalRow("rec-fixture-008", "journal.passed", "journal.stagingMissing"),
  journalRow("rec-fixture-007", "journal.failed", "journal.stagingNotCreated"),
  journalRow("rec-fixture-006", "journal.failed", "journal.stagingNotCreated"),
  journalRow("rec-fixture-005", "journal.passed", "journal.stagingAvailable"),
  journalRow("rec-fixture-004", "journal.failed", "journal.stagingNotCreated"),
  journalRow("rec-fixture-003", "journal.failed", "journal.stagingNotCreated"),
  journalRow("rec-fixture-002", "journal.passed", "journal.stagingAvailable"),
  journalRow("rec-fixture-001", "journal.auditMissing", "journal.stagingOrphan"),
];

/** Latest-10 staging fixture rows correlated by id where applicable. */
export const FIXTURE_STAGING: JournalRow[] = [
  journalRow("rec-fixture-010", "journal.staged", null),
  journalRow("rec-fixture-008", "journal.stagingGone", null),
  journalRow("rec-fixture-005", "journal.staged", null),
  journalRow("rec-fixture-002", "journal.staged", null),
  journalRow("rec-fixture-000", "journal.stagingOrphan", null),
];
