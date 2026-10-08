/**
 * Transport proposals: proposed launcher wire format for future integration
 * ONLY — NOT implemented, no HTTP handlers. Documented as API/SSE contract.
 */
import type { AuditStorageCategory } from "./backend";

/** @category transport-proposal */
export type FolderPurpose =
  | "vault"
  | "staging"
  | "audit"
  | "vector_db"
  | "local_llm"
  | "embedding_model";

/** @category transport-proposal */
export type FolderPickStatus =
  | "configured"
  | "invalid"
  | "unavailable"
  | "cancelled";

/** @category transport-proposal — POST /api/folders/pick */
export interface FolderPickRequest {
  purpose: FolderPurpose;
}

/** @category transport-proposal — POST /api/folders/pick response */
export interface FolderPickResponse {
  purpose: FolderPurpose;
  status: FolderPickStatus;
  /** Backend-computed safe display label; never an authority signal. */
  display: string;
  reason: string;
  validated_at: string;
}

/** @category transport-proposal — GET /api/health response */
export interface HealthResponse {
  app: "living-authenticity";
  version: string;
  instance_id: string;
}

/** @category transport-proposal — POST /api/run */
export interface RunRequest {
  input_text: string;
  source_mode: "personal_vault" | "examples";
  app_language: "en" | "fa";
  staging_language: "en" | "fa";
  audit_language: "en" | "fa";
}

/** @category transport-proposal — POST /api/run response */
export interface RunResponse {
  run_id: string;
  accepted: boolean;
  reason: string;
}

/** @category transport-proposal — POST /api/approve */
export interface ApproveRequest {
  proposal_hash: string;
  query_unit_id: string;
  /** Explicit UI decision; maps to backend y/yes-equivalent semantics. */
  decision: "approve" | "reject";
}

/** @category transport-proposal — POST /api/approve response */
export interface ApproveResponse {
  proposal_hash: string;
  approved: boolean;
  reason: string;
}

/** @category transport-proposal — GET /api/journals item */
export interface JournalItem {
  record_id: string;
  run_id: string;
  proposal_hash: string;
  timestamp: string;
  storage_category: AuditStorageCategory;
  outcome: string;
  artifact_reference: string;
  staging_available: boolean;
}

/** @category transport-proposal — GET /api/journals response */
export interface JournalsResponse {
  audits: JournalItem[];
  staging: JournalItem[];
}

/**
 * @category transport-proposal — GET /api/events (SSE) envelope.
 * Every run-scoped event carries run_id. Content is user-safe operational
 * info only: never private LLM reasoning, secrets, or raw tracebacks.
 */
export type RunEventKind =
  | "run_started"
  | "stage_started"
  | "stage_completed"
  | "status"
  | "warning"
  | "error"
  | "stopped"
  | "human_review_required"
  | "completed";

/** @category transport-proposal */
export interface RunEvent {
  kind: RunEventKind;
  run_id: string;
  stage: string;
  message: string;
  timestamp: string;
}
