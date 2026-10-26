/**
 * Launcher wire format (Increment 2, implemented).
 * Served by src/living_authenticity/adapter/server.py on 127.0.0.1 only:
 * GET /api/health, GET /api/journals, GET /api/events (SSE),
 * POST /api/folders/pick, POST /api/run, POST /api/approve,
 * POST /api/consent, POST /api/cancel, POST /api/proposal/edit.
 */
import type { AuditStorageCategory, ProposalAction } from "./backend";

/** @category transport */
export type FolderPurpose =
  | "vault"
  | "staging"
  | "audit"
  | "vector_db"
  | "local_llm"
  | "embedding_model";

/** @category transport */
export type FolderPickStatus =
  | "configured"
  | "invalid"
  | "unavailable"
  | "cancelled";

/** @category transport — POST /api/folders/pick */
export interface FolderPickRequest {
  purpose: FolderPurpose;
}

/** @category transport — POST /api/folders/pick response */
export interface FolderPickResponse {
  purpose: FolderPurpose;
  status: FolderPickStatus;
  /** Backend-computed safe display label; never an authority signal. */
  display: string;
  reason: string;
  validated_at: string;
}

/** @category transport — GET /api/health response */
export interface HealthResponse {
  app: "living-authenticity";
  version: string;
  instance_id: string;
}

/** @category transport — POST /api/run */
export interface RunRequest {
  input_text: string;
  source_mode: "personal_vault" | "examples";
  /** Requested retrieval mode; backend validates against registry and fails safely when unavailable. */
  retrieval_mode?: "lexical" | "semantic" | "hybrid";
  app_language: "en" | "fa";
  staging_language: "en" | "fa";
  audit_language: "en" | "fa";
  source_type?: string;
  input_id?: string;
}

/** @category transport — POST /api/run response */
export interface RunResponse {
  run_id: string;
  accepted: boolean;
  reason: string;
  /** Effective retrieval actually used for this run. */
  retrieval_mode?: "lexical" | "semantic" | "hybrid";
  source_type?: string;
  input_id?: string;
}

/** @category transport — POST /api/approve */
export interface ApproveRequest {
  proposal_hash: string;
  query_unit_id: string;
  /** Explicit UI decision; maps to backend y/yes-equivalent semantics. */
  decision: "approve" | "reject";
}

/** @category transport — POST /api/approve response */
export interface ApproveResponse {
  proposal_hash: string;
  approved: boolean;
  reason: string;
}

/** @category transport — POST /api/consent (per-call cloud consent) */
export interface ConsentRequest {
  consent_id: string;
  /** Explicit UI decision for exactly one pending consent. */
  decision: "approve" | "reject";
}

/** @category transport — POST /api/consent response */
export interface ConsentResponse {
  consent_id: string;
  approved: boolean;
}

/** @category transport — POST /api/proposal/edit (server-side rehash) */
export interface EditProposalRequest {
  title?: string;
  body?: string;
  reason?: string;
  /** Accepted for interface compatibility; ignored/non-authoritative. */
  proposal_hash?: string;
}

/** @category transport — POST /api/proposal/edit response */
export interface EditProposalResponse {
  proposal_hash: string;
  supersedes: string;
  query_unit_id: string;
  proposal_action: ProposalAction;
  title: string;
  body: string;
  confidence_level: string;
  review_state: string;
  reason: string;
}

/** @category transport — POST /api/cancel (cooperative run cancellation) */
export interface CancelRequest {
  run_id: string;
}

/** @category transport — POST /api/cancel response */
export interface CancelResponse {
  run_id: string;
  status: "cancel_requested" | "already_completed";
  reason: string;
}

/** @category transport — GET /api/journals item */
export interface JournalItem {
  record_id: string;
  run_id: string;
  proposal_hash: string;
  timestamp: string;
  storage_category: AuditStorageCategory;
  outcome: string;
  artifact_reference: string;
  artifact_path?: string;
  staging_available: boolean;
  digest?: string;
  title?: string;
  summary?: string;
  excerpt?: string;
  narrative?: string;
  narrative_language?: string;
  emergency?: boolean;
}

/** @category transport — GET /api/journals response */
export interface JournalsResponse {
  audits: JournalItem[];
  staging: JournalItem[];
}

/** @category transport — GET /api/state response */
export interface StateResponse {
  app: string;
  version: string;
  instance_id: string;
  modes: {
    source_mode: "personal_vault" | "examples";
    retrieval_mode: "lexical" | "semantic";
    app_language: "en" | "fa";
    staging_language: "en" | "fa";
    audit_language: "en" | "fa";
  };
  folders: Record<FolderPurpose, {
    status: FolderPickStatus;
    display: string;
    configured: boolean;
  }>;
  llm: {
    provider: string;
    model: string;
    endpoint: string;
    local_available: boolean;
    cloud_available: boolean;
  };
  active_run_id: string;
  paused_run: {
    run_id: string;
    input_text: string;
    source_mode: string;
    retrieval_mode: string;
    error: string;
    error_message: string;
    timestamp: string;
  } | null;
  pending_sync_approval?: PendingSyncApprovalInfo | null;
}

/** @category transport — Vault change diff */
export interface VaultChangeDiff {
  added: string[];
  modified: string[];
  removed: string[];
  unchanged?: string[];
  renamed_hint?: Array<[string, string]>;
  stale: boolean;
}

/** @category transport — Pending Vault sync approval */
export interface PendingSyncApprovalInfo {
  run_id: string;
  vault_id: string;
  diff: VaultChangeDiff;
  message: string;
  created_at: string;
}

/** @category transport — POST /api/vault/sync/decide */
export interface VaultSyncDecideRequest {
  run_id: string;
  vault_id: string;
  decision: "approve" | "reject";
}

export interface VaultSyncDecideResponse {
  status: "ok";
  run_id: string;
  vault_id: string;
  decision: "approve" | "reject";
}

/** @category transport — POST /api/translate */
export interface TranslateRequest {
  text: string;
  target_lang?: "en" | "fa";
  source_lang?: string;
  source_kind?: string;
  source_id?: string;
}

export interface TranslateResponse {
  translated_text: string;
  target_lang: string;
  source_kind?: string;
  source_id?: string;
  source_language?: string;
  target_language?: string;
  source_text?: string;
  status?: "current" | "stale" | "missing" | "invalid" | "unavailable" | "failed" | "unverifiable" | string;
  source_hash?: string;
  translated_hash?: string;
  translator?: string;
  translated_at?: string;
  version?: string;
  persisted?: boolean;
}

/** @category transport — POST /api/folders/reveal */
export interface RevealFolderResponse {
  purpose: FolderPurpose;
  display: string;
  items: Array<{
    name: string;
    is_dir: boolean;
    size: number;
    modified: string;
  }>;
}

/** @category transport — POST /api/vault/index */
export interface VaultIndexResponse {
  indexed: boolean;
  files_discovered: number;
  notes_indexed: number;
  files_skipped: number;
  reason?: string;
  vault_id?: string;
  status?: string;
  last_synced_at?: string;
}

/** @category transport — GET /api/vault/sync/status */
export interface VaultSyncStatusResponse {
  authorized: boolean;
  vault_id: string;
  sync_active: boolean;
  status:
    | "unauthorized"
    | "authorizing"
    | "sync_active"
    | "change_detected"
    | "sync_in_progress"
    | "fresh"
    | "stale"
    | "error"
    | "revoked";
  last_synced_at?: string;
  indexed_notes: number;
  indexed_vectors: number;
  error?: string | null;
}

/** @category transport — POST /api/run/accept request */
export interface AcceptProposalRequest {
  proposal_hash: string;
  run_id?: string;
}

/** @category transport — POST /api/run/accept */
export interface AcceptProposalResponse {
  accepted: boolean;
  action: string;
  run_id: string;
  proposal_hash?: string;
}

/**
 * @category transport — GET /api/events (SSE) envelope.
 * Every run-scoped event carries run_id. Content is user-safe operational
 * info only: never private LLM reasoning, secrets, or raw tracebacks.
 * Kinds cover run/stage lifecycle, live approval + consent gates, and
 * observational backend results (revalidation/execution).
 */
export type RunEventKind =
  | "run_started"
  | "run_completed"
  | "run_rejected"
  | "stage_started"
  | "stage_completed"
  | "status"
  | "warning"
  | "error"
  | "stopped"
  | "human_review_required"
  | "approval_decided"
  | "consent_required"
  | "revalidation_result"
  | "execution_result"
  | "completed"
  | "sync_approval_required";

/** @category transport */
export interface RunEvent {
  kind: RunEventKind;
  run_id: string;
  stage: string;
  operation?: string;
  message: string;
  timestamp: string;
  proposal_hash?: string;
  query_unit_id?: string;
  proposal_action?: ProposalAction;
  title?: string;
  body?: string;
  confidence_level?: string;
  confidence_score?: number;
  confidence_score_band?: string;
  classification_type?: string;
  consent_id?: string;
  outcome?: "ACCEPTED" | "REJECTED" | "NEEDS_CLARIFICATION" | "STOPPED" | "ERROR";
  error_code?: string;
  recovery_options?: string[];
  input_id?: string;
  input_hash?: string;
  vault_id?: string;
  diff?: VaultChangeDiff;
  terminal_outcome?: string;
  reason?: string;
  status?: string;
  phase?: "started" | "completed" | "failed" | "cancelled" | "stopped";
}
