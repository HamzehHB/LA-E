/**
 * Contract classification.
 *
 * Every type in this folder header carries a `@category` tag:
 * - backend-dto: mirrors an existing frozen Python dataclass (field names preserved).
 * - transport-proposal: proposed launcher wire format for future integration (NOT yet implemented).
 * - ui-model: frontend-only presentation state.
 * - fixture: synthetic demo data.
 *
 * Backend sources of truth:
 * - Proposal: src/living_authenticity/knowledge/decision/proposal/outcome.py
 * - ConfidenceAssessment: src/living_authenticity/knowledge/decision/confidence/outcome.py
 * - FilterOutcome: src/living_authenticity/knowledge/decision/filter_outcome.py
 * - ApprovalRequest/Outcome: src/living_authenticity/knowledge/governance/approval/outcome.py
 * - IntegrationUnitEntry/RunReport: src/living_authenticity/knowledge/integration/outcome.py
 * - AuditRecord: src/living_authenticity/knowledge/governance/audit/outcome.py
 * - STAGE_ORDER: src/living_authenticity/knowledge/orchestration/orchestrator.py
 */

/** @category backend-dto — mirrors PROPOSAL_ACTIONS in proposal/outcome.py */
export type ProposalAction = "CREATE" | "DO_NOT_IMPORT" | "NEEDS_REVIEW";

/** @category backend-dto — mirrors CONFIDENCE_LEVELS in confidence/outcome.py (ordinal, NOT numeric) */
export type ConfidenceLevel =
  | "sufficiently_supported"
  | "weak"
  | "contradictory_or_unresolved"
  | "insufficient_evidence";

/** @category backend-dto — mirrors FILTER_VERDICTS in filter_outcome.py */
export type FilterVerdict = "pass_to_review" | "hold_for_review" | "do_not_import";

/** @category backend-dto — mirrors STAGE_ORDER in orchestrator.py.
 *  NOTE: "representation" is part of the real backend STAGE_ORDER tuple; the
 *  The presentation-only workflow keeps all 10 backend stages in the domain contract and only
 *  groups them visually (see STAGE_GROUPS in workflow.ts). */
export type BackendStage =
  | "ingestion"
  | "retrieval"
  | "comparison"
  | "relation"
  | "core"
  | "classification"
  | "proposal"
  | "confidence"
  | "knowledge_filter"
  | "representation";

/** @category backend-dto — mirrors Proposal fields */
export interface ProposalDto {
  query_unit_id: string;
  query_source: string;
  query_position: number;
  action: ProposalAction;
  title: string;
  destination: string;
  reason: string;
  evidence: string[];
  relevant_candidates: string[];
  uncertainties: string[];
  provenance: string[];
  classification_type: string;
  comparison_summary: string;
  relation_summary: string;
  core_relevance: string;
  strategy: string;
  note: string;
  is_authoritative: false;
  requires_human_review: true;
}

/** @category backend-dto — mirrors ConfidenceAssessment fields */
export interface ConfidenceDto {
  query_unit_id: string;
  query_source: string;
  query_position: number;
  proposal_action: string;
  level: ConfidenceLevel;
  strategy: string;
  basis: string;
  uncertainties: string[];
  note: string;
  is_authoritative: false;
  requires_human_review: true;
}

/** @category backend-dto — mirrors FilterOutcome fields */
export interface FilterOutcomeDto {
  query_unit_id: string;
  query_source: string;
  query_position: number;
  verdict: FilterVerdict;
  recommended_action: ProposalAction;
  basis: string;
  uncertainties: string[];
  evidence: string[];
  strategy: string;
  note: string;
  is_authoritative: false;
  requires_human_review: true;
}

/** @category backend-dto — mirrors IntegrationUnitEntry fields */
export interface IntegrationUnitEntryDto {
  source_file: string;
  query_unit_id: string;
  query_source: string;
  query_position: number;
  proposal_hash: string;
  proposal_action: string;
  approval_approved: boolean;
  revalidation_valid: boolean;
  execution_attempted: boolean;
  execution_permitted: boolean;
  execution_executed: boolean;
  outcome: string;
  reason: string;
  artifact_reference: string;
  query_text: string;
  proposal_title: string;
  proposal_body: string;
  proposal_reason: string;
  proposed_type: string;
  uncertainty: string;
  note_markdown: string;
  proposed_destination: string;
  llm_candidates: unknown[];
  confidence_level: string;
  confidence_basis: string;
  filter_verdict: string;
  filter_basis: string;
  retrieval_candidates: unknown[];
  comparison_results: unknown[];
  relation_results: unknown[];
  core_relevance: string;
  core_basis: string;
  classification_type: string;
  classification_rationale: string;
}

/** @category backend-dto — mirrors IntegrationRunReport fields */
export interface IntegrationRunReportDto {
  source_root: string;
  staging_root: string;
  max_files: number;
  files_seen: number;
  files_processed: number;
  files_skipped: number;
  accepted: boolean;
  rejection_reason: string;
  rejection_check: string;
  units: IntegrationUnitEntryDto[];
  audits: unknown[];
  details: unknown[];
  audit_receipts: unknown[];
  is_authoritative: false;
  requires_human_review: true;
}

/** @category backend-dto — audit storage partition (persistence.py: passed|failed) */
export type AuditStorageCategory = "passed" | "failed";

/**
 * @category backend-dto — §5 audit-root routing (mirrors persistence.py).
 * The user-selected audit root holds passed/ + failed/ subdirectories,
 * created by the backend (AuditWriter makedirs), never by React.
 * passed = ONLY executed-CREATE terminal records (execution_executed);
 * failed = every other terminal outcome (rejected/held/safe-stopped/failures,
 * including pre-execution checkpoints).
 */
export interface AuditRootLayout {
  auditRootDisplay: string;
  passedSubdir: "passed";
  failedSubdir: "failed";
}

/**
 * @category backend-dto — §2 representation clarification (verified).
 * `representation` IS a real backend pipeline stage: STAGE_ORDER's 10th entry
 * (orchestrator.py), executed by DefaultOutputGenerator.generate() which renders
 * the deterministic proposed-note markdown (evidence_note.py) into
 * GeneratedNote. It performs no FS/network/model work and authorizes nothing;
 * it is an analytical rendering step, not a synonym for the UI itself.
 */
export type RepresentationStageNote =
  "representation renders the proposed note; it does not display the UI";

