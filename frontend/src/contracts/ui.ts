/**
 * UI/view models + error model + config-state model.
 * Frontend-only presentation state. Never authoritative backend DTOs.
 */
import type {
  ConfidenceLevel,
  FilterVerdict,
  ProposalAction,
} from "./backend";
import type { FolderPurpose } from "./transport";

/** @category ui-model — typed error categories */
export type AppErrorKind =
  | "configuration_error"
  | "validation_error"
  | "backend_error"
  | "processing_error"
  | "user_cancellation"
  | "unavailable_resource"
  | "connection_unavailable"
  | "unknown_error";

/** @category ui-model */
export interface AppError {
  kind: AppErrorKind;
  /** User-safe message (localized key resolved at render). */
  messageKey: string;
  detail?: string;
}

/** @category ui-model — six-state config validity */
export type ConfigValidity =
  | "not_configured"
  | "configured_valid"
  | "configured_unavailable"
  | "configured_invalid"
  | "validation_pending"
  | "validation_error";

/** @category ui-model */
export interface ConfigResourceState {
  purpose: FolderPurpose;
  validity: ConfigValidity;
  display: string;
  reason: string;
}

/** @category ui-model */
export type SourceMode = "test" | "vault";

/** @category ui-model */
export type Theme = "light" | "dark";

/** @category ui-model */
export type AppLanguage = "en" | "fa";

/** @category ui-model */
export type RetrievalType = "lexical" | "semantic";

/** @category ui-model */
export type LlmMode = "local" | "cloud";

/** @category ui-model */
export type StageVisualState =
  | "completed"
  | "current"
  | "pending"
  | "stopped";

/**
 * @category ui-model — §6 workflow synchronization states (driven by future live runtime).
 * Foundation fixtures only use completed/current/pending/stopped; running/blocked/
 * failed are represented in mock state for contract readiness.
 */
export type WorkflowSyncState =
  | "pending"
  | "running"
  | "completed"
  | "blocked"
  | "failed"
  | "stopped";

/** @category ui-model */
export interface WorkflowStageView {
  /** Backend stage key (1:1 with BackendStage; groups reference subsets). */
  backendStage: string;
  labelKey: string;
  state: StageVisualState;
  group?: string;
}

/** @category ui-model — mock human-review state (presentation-only) */
export type HumanReviewState =
  | "awaiting_review"
  | "mock_approved"
  | "mock_rejected"
  | "mock_edited_stale";

/** @category ui-model — current processing/run view */
export interface CurrentRunView {
  run_id: string;
  current_stage: string;
  status: string;
  proposal_action: ProposalAction;
  proposal_title: string;
  proposal_body: string;
  confidence_level: ConfidenceLevel;
  filter_verdict: FilterVerdict;
  proposal_hash: string;
  review_state: HumanReviewState;
  edited_body: string | null;
  error: AppError | null;
  stopped: boolean;
  stop_reason: string;
}

/** @category ui-model — journal row (rendered from fixture) */
export interface JournalRow {
  id: string;
  timestamp: string;
  statusKey: string;
  stagingNoteKey: string | null;
}

/** @category ui-model */
export interface AppState {
  theme: Theme;
  appLanguage: AppLanguage;
  stagingLanguage: AppLanguage;
  auditLanguage: AppLanguage;
  knowledgeLanguageNote: string;
  sourceMode: SourceMode;
  retrievalType: RetrievalType;
  llmMode: LlmMode;
  resources: Record<FolderPurpose, ConfigResourceState>;
  run: CurrentRunView;
  recentAudits: JournalRow[];
  recentStaging: JournalRow[];
  composerOpen: boolean;
  composerText: string;
  journalIndex: number;
  /** @category ui-model — §§3–4, 7–11 vector-index lifecycle (fixtures only) */
  vectorIndex: VectorIndexState;
  /** @category ui-model — §10 vector engine install state (fixtures only) */
  vectorEngine: VectorEngineState;
  /** @category ui-model — §11 embedding-model state (fixtures only) */
  embeddingModel: EmbeddingModelState;
  /** @category ui-model — §§13–14 LLM availability (fixtures only) */
  llmAvailability: LlmAvailability;
  /** @category ui-model — §§7–8, 17–18 onboarding decisions (fixtures only) */
  onboarding: OnboardingState;
  /** @category ui-model — §§15–16, 19–21 safe-stop/fix/resume (fixtures only) */
  safeStop: SafeStopState | null;
}

/**
 * @category ui-model — §4 vector-index lifecycle.
 * Path exists ≠ usable; model configured ≠ embeddings available;
 * embedded ≠ authorized for indexing (see IndexingEligibility).
 */
export type VectorIndexStatus =
  | "not_configured"
  | "engine_unavailable"
  | "configured_empty"
  | "initializing"
  | "ready"
  | "updating"
  | "failed"
  | "stale_or_requires_rebuild";

/** @category ui-model */
export interface VectorIndexState {
  status: VectorIndexStatus;
  engine: string;
  baselineExists: boolean;
  reason: string;
}

/**
 * @category ui-model — §3 indexing eligibility (future-integration contract preview).
 * Only eligible staged/approved artifacts may enter the vector store.
 * Filename, location, timestamp, similarity, confidence, parsing success,
 * retrieval, classification, and proposal NEVER independently authorize.
 */
export interface IndexingEligibility {
  eligible: boolean;
  sourceIdentity: string;
  runId: string;
  provenance: string[];
  artifactIdentity: string;
  approvalState: string;
  revalidationState: string;
  reason: string;
}

/** @category ui-model — §10 vector engine install lifecycle (mock only) */
export type VectorEngineStatus =
  | "missing"
  | "install_available"
  | "user_declined"
  | "installing"
  | "ready"
  | "alternate_configured";

/** @category ui-model */
export interface VectorEngineState {
  status: VectorEngineStatus;
  recommended: "lancedb";
  selected: string;
}

/** @category ui-model — §11 embedding-model selection (mock only) */
export interface EmbeddingModelState {
  recommended: "bge_m3";
  selected: string;
  /** Never true unless the configured model is actually available. */
  available: boolean;
}

/**
 * @category ui-model — §§13–14, 16 LLM availability (mock only).
 * Local: not_installed | path_missing | path_invalid | runtime_not_running | ready.
 * Cloud: unavailable | invalid | available.
 */
export type LocalLlmStatus =
  | "not_installed"
  | "path_missing"
  | "path_invalid"
  | "runtime_not_running"
  | "ready";

/** @category ui-model */
export type CloudLlmStatus = "unavailable" | "invalid" | "available";

/** @category ui-model */
export interface LlmAvailability {
  local: LocalLlmStatus;
  cloud: CloudLlmStatus;
}

/** @category ui-model — §§7–8, 17–18 first-use onboarding (mock only) */
export interface OnboardingState {
  vectorBaselineOffered: boolean;
  vectorBaselineAccepted: boolean | null;
  vectorBaselineDismissed: boolean;
  engineOffered: boolean;
  engineDismissed: boolean;
}

/**
 * @category ui-model — §§15–16, 19–21 safe-stop (mock only).
 * Fix resumes the SAME run_id after backend revalidation; Stop ends it
 * as a terminal auditable safe-stopped state. Color is supplementary;
 * meaning is carried by labels + state.
 */
export interface SafeStopState {
  runId: string;
  stage: string;
  reason: string;
  fixLabelKey: string;
  fixTarget: string;
  userChoice: "undecided" | "fix" | "stop";
  resumed: boolean;
}
