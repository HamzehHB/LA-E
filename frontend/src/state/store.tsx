import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { AppState, ConfigResourceState, CurrentRunView } from "../contracts/ui";
import type {
  FolderPurpose,
  PendingSyncApprovalInfo,
  RunEvent,
  TranslateResponse,
  VaultChangeDiff,
  VaultSyncStatusResponse,
} from "../contracts/transport";
import { FIXTURE_AUDITS, FIXTURE_RUN, FIXTURE_STAGING, IDLE_RUN } from "../fixtures/demo";
import { createLiveClient, type LiveClient } from "../live/client";


function readBool(key: string): boolean {
  try {
    return localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function initialResources(): AppState["resources"] {
  const purposes: FolderPurpose[] = ["vault", "staging", "audit", "vector_db", "local_llm", "embedding_model"];
  const entries = purposes.map((purpose): [FolderPurpose, ConfigResourceState] => [
    purpose,
    { purpose, validity: "not_configured", display: "", reason: "" },
  ]);
  return Object.fromEntries(entries) as AppState["resources"];
}

const initialState: AppState = {
  theme: "light",
  appLanguage: "en",
  stagingLanguage: "en",
  auditLanguage: "en",
  knowledgeLanguageNote: "knowledge follows source",
  sourceMode: "test",
  retrievalType: "lexical",
  llmMode: "local",
  resources: initialResources(),
  run: { ...FIXTURE_RUN },
  runEvents: [],
  recentAudits: [...FIXTURE_AUDITS],
  recentStaging: [...FIXTURE_STAGING],
  composerOpen: false,
  composerText: "",
  journalIndex: 0,
  welcomed: readBool("lae-welcomed"),
  vectorIndex: { status: "not_configured", engine: "", baselineExists: false, reason: "" },
  vectorEngine: { status: "missing", recommended: "lancedb", selected: "" },
  embeddingModel: { recommended: "bge_m3", selected: "", available: false },
  llmAvailability: { local: "not_installed", cloud: "unavailable" },
  onboarding: {
    vectorBaselineOffered: true,
    vectorBaselineAccepted: null,
    vectorBaselineDismissed: false,
    engineOffered: true,
    engineDismissed: false,
  },
  safeStop: null,
  lastSubmittedText: "",
  inputOutcome: null,
  inputErrorCode: null,
  inputRecoveryOptions: [],
};

interface Store {
  state: AppState;
  setTheme: (t: AppState["theme"]) => void;
  setAppLanguage: (l: AppState["appLanguage"]) => void;
  setStagingLanguage: (l: AppState["stagingLanguage"]) => void;
  setAuditLanguage: (l: AppState["auditLanguage"]) => void;
  setSourceMode: (m: AppState["sourceMode"]) => void;
  setRetrievalType: (r: AppState["retrievalType"]) => void;
  setLlmMode: (m: AppState["llmMode"]) => void;
  mockApprove: () => void;
  mockReject: () => void;
  mockEdit: (body: string) => void;
  setComposer: (open: boolean, text?: string) => void;
  rotateJournal: () => void;
  stepJournal: (delta: number) => void;
  setWelcomed: () => void;
  mockSafeStop: (stage: string, reason: string, fixTarget: string) => void;
  mockFix: () => void;
  mockStopProcess: () => void;
  mockSubmitInput: (text: string) => void;
  mockOnboardingChoice: (key: "vectorBaselineAccepted", value: boolean | null) => void;
  mockDismissOnboarding: (key: "vectorBaselineDismissed" | "engineDismissed") => void;
  mockEngineChoice: (status: AppState["vectorEngine"]["status"], selected?: string) => void;
  /** Live launcher connection (Increment 1). No-op when unavailable. */
  liveAvailable: boolean;
  liveConnected: boolean;
  liveError: string | null;
  startLiveRun: (inputText: string) => Promise<void>;
  fixInput: () => void;
  retryLastInput: () => Promise<void>;
  dismissRunOutcome: () => void;
  cancelActiveRun: () => Promise<void>;
  liveApprove: (decision: "approve" | "reject") => Promise<void>;
  liveEditProposal: (title: string, body: string, reason: string) => Promise<void>;
  liveAccept: () => Promise<void>;
  liveFix: (inputText?: string) => Promise<void>;
  liveStop: () => Promise<void>;
  liveTranslate: (
    text: string,
    target_lang?: "en" | "fa",
    source_lang?: string,
    source_kind?: string,
    source_id?: string,
  ) => Promise<TranslateResponse>;
  liveIndexVault: () => Promise<{ indexed: boolean; reason?: string }>;
  liveAuthorizeVaultSync: (vaultRoot?: string) => Promise<{ indexed: boolean; reason?: string; vault_id?: string; status?: string }>;
  liveRevokeVaultSync: () => Promise<{ authorized: boolean; status: string }>;
  liveReconcileVaultSync: () => Promise<unknown>;
  liveGetVaultSyncStatus: () => Promise<VaultSyncStatusResponse | null>;
  liveRevealFolder: (purpose: FolderPurpose) => Promise<Array<{ name: string; is_dir: boolean; size: number; modified: string }>>;
  liveConsent: (decision: "approve" | "reject") => Promise<void>;
  liveDecideVaultSync: (decision: "approve" | "reject") => Promise<void>;
  liveCancel: () => Promise<void>;
  refreshJournals: () => Promise<void>;
  /** Open the native folder dialog for one purpose via the launcher. */
  pickLiveFolder: (purpose: FolderPurpose) => Promise<void>;
  retryConnection: () => Promise<void>;
  dismissLiveError: () => void;
  /** Apply a stream event to update store state and lifecycle. */
  applyEvent: (event: RunEvent) => void;
  actionInFlight: boolean;
  /** Pending live payloads surfaced from the SSE stream. */
  liveApproval: {
    proposal_hash: string;
    query_unit_id: string;
    proposal_action: string;
    title: string;
    body: string;
    confidence_level: string;
    confidence_score?: number;
    confidence_score_band?: string;
    classification_type?: string;
    destination?: string;
    reason?: string;
    evidence?: string[];
    provenance?: string[];
    uncertainties?: string[];
    is_executable?: boolean;
  } | null;
  liveConsentRequest: { consent_id: string; message: string } | null;
  pendingSyncApproval: PendingSyncApprovalInfo | null;
}

export const StoreContext = createContext<Store | null>(null);
const Ctx = StoreContext;

const THEME_KEY = "lae-theme";
const WELCOME_KEY = "lae-welcomed";
const MAX_RUN_EVENTS = 200;

export function StoreProvider({ children }: { children: ReactNode }) {
  // Live client: created once, token stays in module memory (never stored).
  const [live] = useState<LiveClient>(() => createLiveClient());
  const [state, setState] = useState<AppState>(() => {
    let theme = initialState.theme;
    let welcomed = false;
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "dark" || saved === "light") {
        theme = saved;
      }
      welcomed = localStorage.getItem(WELCOME_KEY) === "1";
    } catch {
      /* ignore */
    }
    const initialRun = live.available ? { ...IDLE_RUN } : { ...FIXTURE_RUN };
    const initialAudits = live.available ? [] : [...FIXTURE_AUDITS];
    const initialStaging = live.available ? [] : [...FIXTURE_STAGING];
    return {
      ...initialState,
      theme,
      welcomed,
      run: initialRun,
      recentAudits: initialAudits,
      recentStaging: initialStaging,
    };
  });
  const [liveConnected, setLiveConnected] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [actionInFlight, setActionInFlight] = useState(false);
  const [liveApproval, setLiveApproval] = useState<Store["liveApproval"]>(null);
  const [liveConsentRequest, setLiveConsentRequest] = useState<
    Store["liveConsentRequest"]
  >(null);
  const [pendingSyncApproval, setPendingSyncApproval] = useState<PendingSyncApprovalInfo | null>(null);
  const lastInstanceIdRef = useRef<string>("");
  const currentRunIdRef = useRef<string>(live.available ? "" : "run-fixture-001");
  const isStoppedRef = useRef<boolean>(false);
  const previousRunIdRef = useRef<string>("");
  const retiredRunIdsRef = useRef<Set<string>>(new Set());

  const retireRunId = (id: string) => {
    if (id && !id.startsWith("run-fixture")) {
      retiredRunIdsRef.current.add(id);
      previousRunIdRef.current = id;
    }
  };

  const stateRef = useRef(state);
  stateRef.current = state;

  const applyEvent = (event: RunEvent) => {
    const runId = typeof event.run_id === "string" ? event.run_id : "";
    const stage = typeof event.stage === "string" ? event.stage : "";
    const message = typeof event.message === "string" ? event.message : "";
    const extra = event as RunEvent & Record<string, unknown>;

    const activeRunId = currentRunIdRef.current;
    const isFixtureOrIdle = !activeRunId || activeRunId.startsWith("run-fixture");

    // 1. Run identity correlation:
    // When an active real run exists, every lifecycle and decision event must carry the exact matching run_id.
    // Events lacking a run_id or carrying a foreign run_id are rejected before state mutation or connection updates.
    if (!isFixtureOrIdle && (!runId || runId !== activeRunId)) {
      return;
    }

    // 2. Retired/previous-run rejection: If the event carries any previously retired run ID, it is stale.
    if (runId && (retiredRunIdsRef.current.has(runId) || (previousRunIdRef.current && runId === previousRunIdRef.current))) {
      return;
    }

    // 3. Idle / fixture gate: When no real run is active, ONLY a legitimate run_started event
    // with a valid non-empty run_id can initialize a new run. Ordinary events or events without
    // run_id cannot initialize or mutate the run state.
    if (isFixtureOrIdle && (event.kind !== "run_started" || !runId)) {
      return;
    }

    // 4. Stale event protection on stopped runs: If the active run has been stopped or rejected,
    // prevent delayed lifecycle events from reviving it to running.
    if (!isFixtureOrIdle && isStoppedRef.current && (!runId || runId === activeRunId)) {
      if (
        event.kind !== "stopped" &&
        event.kind !== "run_rejected" &&
        event.kind !== "error" &&
        event.kind !== "warning" &&
        event.kind !== "audit_persisted" &&
        event.kind !== "audit_failed"
      ) {
        return;
      }
    }

    // Connection-state ordering: Event is verified as valid for the current/new run;
    // mark connection live now without allowing rejected foreign/stale events to do so.
    setLiveConnected(true);

    if (event.kind === "run_started" && runId) {
      if (currentRunIdRef.current && currentRunIdRef.current !== runId) {
        retireRunId(currentRunIdRef.current);
      }
      currentRunIdRef.current = runId;
      isStoppedRef.current = false;
    } else if (
      event.kind === "stopped" ||
      event.kind === "run_rejected" ||
      event.kind === "error"
    ) {
      isStoppedRef.current = true;
    }

    // Retain a bounded, run-scoped event log for Current Audit + timeline.
    setState((s) => {
      const rid = typeof event.run_id === "string" ? event.run_id : "";
      const isFixture = !s.run.run_id || s.run.run_id.startsWith("run-fixture");
      if (!isFixture && (!rid || rid !== s.run.run_id)) {
        return s;
      }
      if (rid && (retiredRunIdsRef.current.has(rid) || (previousRunIdRef.current && rid === previousRunIdRef.current))) {
        return s;
      }
      if (isFixture && event.kind !== "run_started") {
        return s;
      }
      const baseEvents = isFixture ? [] : s.runEvents;
      const next = baseEvents.length >= MAX_RUN_EVENTS
        ? [...baseEvents.slice(baseEvents.length - MAX_RUN_EVENTS + 1), event]
        : [...baseEvents, event];
      return { ...s, runEvents: next };
    });

    // Stale event protection: If this run has been stopped or rejected,
    // prevent any delayed SSE events from reviving it to RUNNING or AWAITING_REVIEW.
    const isCurFixture = !stateRef.current.run.run_id || stateRef.current.run.run_id.startsWith("run-fixture");
    if (!isCurFixture && stateRef.current.run.stopped && (!runId || runId === stateRef.current.run.run_id)) {
      if (
        event.kind !== "stopped" &&
        event.kind !== "run_rejected" &&
        event.kind !== "error" &&
        event.kind !== "warning"
      ) {
        return;
      }
    }
    if (event.kind === "status" && message.startsWith("retrieval mode:")) {
      const effective = message.includes("semantic") ? "semantic" : "lexical";
      setState((s) => ({
        ...s,
        retrievalType: effective,
        run: { ...s.run, status: message },
      }));
      return;
    }
    if (event.kind === "sync_approval_required") {
      const vaultId = String(extra.vault_id ?? "");
      const diff = extra.diff as VaultChangeDiff | undefined;
      if (runId && diff) {
        if (stateRef.current.run.stopped && (!runId || runId === stateRef.current.run.run_id)) {
          return;
        }
        setPendingSyncApproval({
          run_id: runId,
          vault_id: vaultId,
          diff,
          message: message || "Obsidian Vault changes detected — synchronization requires approval before retrieval",
          created_at: event.timestamp || new Date().toISOString(),
        });
        setState((s) => {
          if (s.run.stopped && (!runId || runId === s.run.run_id)) {
            return s;
          }
          return {
            ...s,
            run: {
              ...s.run,
              run_id: runId || s.run.run_id,
              status: "awaiting Vault synchronization approval",
            },
          };
        });
      }
      return;
    }
    if (event.kind === "human_review_required") {
      const proposalHash = String(extra.proposal_hash ?? "");
      if (proposalHash) {
        if (stateRef.current.run.stopped && (!runId || runId === stateRef.current.run.run_id)) {
          return;
        }
        const confScore = typeof extra.confidence_score === "number"
          ? extra.confidence_score
          : (extra.confidence_score ? Number(extra.confidence_score) : 0.89);
        const confBand = String(extra.confidence_score_band ?? (confScore >= 0.8 ? "high" : confScore >= 0.5 ? "moderate" : "low"));
        const classType = String(extra.classification_type ?? "Observation");
        const action = String(extra.proposal_action ?? "");
        const isExecutable = extra.is_executable !== undefined ? Boolean(extra.is_executable) : action === "CREATE";
        const destination = String(extra.destination ?? "");
        const reason = String(extra.reason ?? "");
        const evidence = Array.isArray(extra.evidence) ? (extra.evidence as string[]) : [];
        const provenance = Array.isArray(extra.provenance) ? (extra.provenance as string[]) : [];
        const uncertainties = Array.isArray(extra.uncertainties) ? (extra.uncertainties as string[]) : [];
        const awaitingStatus = isExecutable
          ? "awaiting explicit approval"
          : "awaiting human review / acknowledgment";

        setLiveApproval({
          proposal_hash: proposalHash,
          query_unit_id: String(extra.query_unit_id ?? ""),
          proposal_action: action,
          title: String(extra.title ?? ""),
          body: String(extra.body ?? ""),
          confidence_level: String(extra.confidence_level ?? ""),
          confidence_score: confScore,
          confidence_score_band: confBand,
          classification_type: classType,
          destination,
          reason,
          evidence,
          provenance,
          uncertainties,
          is_executable: isExecutable,
        });
        setState((s) => {
          if (s.run.stopped && (!runId || runId === s.run.run_id)) {
            return s;
          }
          return {
            ...s,
            run: {
              ...s.run,
              run_id: runId || s.run.run_id,
              proposal_action:
                (action as CurrentRunView["proposal_action"]) ||
                s.run.proposal_action,
              classification_type: classType,
              proposal_title: String(extra.title ?? s.run.proposal_title),
              proposal_body: String(extra.body ?? s.run.proposal_body),
              confidence_score: confScore,
              confidence_score_band: confBand,
              proposal_hash: proposalHash,
              destination,
              reason,
              evidence,
              provenance,
              uncertainties,
              is_executable: isExecutable,
              review_state: "live_awaiting_review",
              current_stage: "human_review",
              status: awaitingStatus,
              error: null,
            },
          };
        });
      }
      return;
    }
    if (event.kind === "consent_required") {
      const consentId = String(extra.consent_id ?? "");
      if (consentId) {
        if (stateRef.current.run.stopped && (!runId || runId === stateRef.current.run.run_id)) {
          return;
        }
        setLiveConsentRequest({ consent_id: consentId, message });
        setState((s) => {
          if (s.run.stopped && (!runId || runId === s.run.run_id)) {
            return s;
          }
          return {
            ...s,
            run: { ...s.run, review_state: "live_consent_required" },
          };
        });
      }
      return;
    }
    if (event.kind === "approval_decided") {
      setLiveApproval(null);
      setState((s) => {
        if (s.run.stopped && (!runId || runId === s.run.run_id)) {
          return s;
        }
        const isApprove = /approve/i.test(message);
        const isReject = /reject/i.test(message);
        const isAck = /acknowledged|ack/i.test(message);
        const nextReviewState = isApprove
          ? "live_approved"
          : isReject
            ? "live_rejected"
            : isAck
              ? "live_acknowledged"
              : s.run.review_state;
        const nextStatus = isApprove
          ? "proposal approved"
          : isReject
            ? "proposal rejected"
            : isAck
              ? "proposal acknowledged"
              : message || s.run.status;
        return {
          ...s,
          run: {
            ...s.run,
            review_state: nextReviewState,
            status: nextStatus,
            accepted: isAck ? true : s.run.accepted,
          },
        };
      });
      return;
    }
    if (
      event.kind === "run_started" ||
      event.kind === "run_completed" ||
      event.kind === "stage_started" ||
      event.kind === "stage_completed" ||
      event.kind === "revalidation_result" ||
      event.kind === "execution_result" ||
      event.kind === "status" ||
      event.kind === "completed"
    ) {
      setState((s) => {
        const isFixture = !s.run.run_id || s.run.run_id.startsWith("run-fixture");
        // Prevent uncorrelated events or events from a different run from corrupting the active run
        if (!isFixture && (!runId || runId !== s.run.run_id)) {
          return s;
        }
        if (runId && (retiredRunIdsRef.current.has(runId) || (previousRunIdRef.current && runId === previousRunIdRef.current))) {
          return s;
        }
        if (isFixture && event.kind !== "run_started") {
          return s;
        }
        // Prevent stale events from reviving a run that was stopped or rejected
        if (s.run.stopped && (!runId || runId === s.run.run_id)) {
          return s;
        }
        return {
          ...s,
          run: {
            ...s.run,
            run_id: runId || s.run.run_id,
            current_stage: stage || (event.kind === "run_started" ? (stage || "input") : s.run.current_stage),
            status: message || s.run.status,
            stopped: false,
            stop_reason: event.kind === "run_started" ? "" : s.run.stop_reason,
            audit_status: event.kind === "run_started" ? "pending" : s.run.audit_status,
          },
        };
      });
      if (event.kind === "completed" || event.kind === "run_completed") {
        setPendingSyncApproval(null);
        void refreshJournalsQuiet();
      }
      return;
    }
    if (event.kind === "audit_persisted") {
      const isEmergency =
        event.audit_mode === "emergency" ||
        event.audit_partition === "errors" ||
        /emergency/i.test(message) ||
        event.operation === "emergency";
      setState((s) => {
        const isFixture = !s.run.run_id || s.run.run_id.startsWith("run-fixture");
        if (!isFixture && (!runId || runId !== s.run.run_id)) {
          return s;
        }
        return {
          ...s,
          run: {
            ...s.run,
            audit_status: isEmergency ? "emergency_persisted" : "normal_persisted",
          },
        };
      });
      void refreshJournalsQuiet();
      return;
    }
    if (event.kind === "audit_failed") {
      setState((s) => {
        const isFixture = !s.run.run_id || s.run.run_id.startsWith("run-fixture");
        if (!isFixture && (!runId || runId !== s.run.run_id)) {
          return s;
        }
        return {
          ...s,
          run: {
            ...s.run,
            audit_status: "persistence_failed",
          },
        };
      });
      return;
    }
    if (event.kind === "warning") {
      setState((s) => {
        const isFixture = !s.run.run_id || s.run.run_id.startsWith("run-fixture");
        // If this warning is uncorrelated or belongs to a different run, ignore it.
        if (!isFixture && (!runId || runId !== s.run.run_id)) {
          return s;
        }
        if (runId && (retiredRunIdsRef.current.has(runId) || (previousRunIdRef.current && runId === previousRunIdRef.current))) {
          return s;
        }
        if (isFixture) {
          return s;
        }
        return {
          ...s,
          run: {
            ...s.run,
            run_id: runId || s.run.run_id,
            current_stage: s.run.stopped
              ? s.run.current_stage
              : (s.run.current_stage || stage || "input"),
            stopped: s.run.stopped,
            stop_reason: s.run.stop_reason,
            status: message || s.run.status,
          },
        };
      });
      return;
    }
    if (
      event.kind === "run_rejected" ||
      event.kind === "stopped" ||
      event.kind === "error"
    ) {
      const isFixture = !stateRef.current.run.run_id || stateRef.current.run.run_id.startsWith("run-fixture");
      if (!isFixture && (!runId || runId !== stateRef.current.run.run_id)) {
        return;
      }
      setPendingSyncApproval(null);
      setLiveApproval(null);
      const outcome = (event.outcome as AppState["inputOutcome"]) ||
        (event.kind === "stopped" ? "STOPPED" : "REJECTED");
      setState((s) => {
        const sIsFixture = !s.run.run_id || s.run.run_id.startsWith("run-fixture");
        if (!sIsFixture && (!runId || runId !== s.run.run_id)) {
          return s;
        }
        return {
          ...s,
          inputOutcome: outcome,
          inputErrorCode: event.error_code ?? null,
          inputRecoveryOptions: event.recovery_options ?? ["fix_input", "stop"],
          run: {
            ...s.run,
            run_id: runId || s.run.run_id,
            current_stage: stage || s.run.current_stage || "input",
            stopped: true,
            stop_reason: message,
            status: message || s.run.status,
          },
        };
      });
      return;
    }
  };

  const refreshJournalsQuiet = async () => {
    if (!live.available) return;
    try {
      const data = await live.journals();
      setState((s) => ({
        ...s,
        recentAudits: (data?.audits || []).map((a) => ({
          id: a.record_id,
          timestamp: a.timestamp,
          statusKey:
            a.outcome === "passed"
              ? "journal.passed"
              : a.outcome === "staged_audit_pending"
                ? "journal.auditPending"
                : a.outcome === "staging_unlinked"
                  ? "journal.stagingOrphan"
                  : "journal.failed",
          stagingNoteKey: a.staging_available
            ? "journal.stagingAvailable"
            : "journal.stagingNotCreated",
          runId: a.run_id || undefined,
          proposalHash: a.proposal_hash || undefined,
          proposal_hash: a.proposal_hash || undefined,
          record_id: a.record_id,
          artifactReference: a.artifact_reference || undefined,
          digest: a.digest,
          title: a.title,
          summary: a.summary,
          staging_available: a.staging_available,
          narrative: a.narrative,
          narrative_language: a.narrative_language,
          emergency: a.emergency,
        })),
        recentStaging: (data?.staging || []).map((r) => ({
          id: r.record_id,
          timestamp: r.timestamp,
          statusKey:
            r.outcome === "staged_audit_pending"
              ? "journal.auditPending"
              : r.outcome === "staging_unlinked"
                ? "journal.stagingOrphan"
                : r.staging_available
                  ? "journal.staged"
                  : "journal.failed",
          stagingNoteKey: null,
          runId: r.run_id || undefined,
          proposalHash: r.proposal_hash || undefined,
          proposal_hash: r.proposal_hash || undefined,
          record_id: r.record_id,
          artifactReference: r.artifact_reference || undefined,
          title: r.title,
          excerpt: r.excerpt,
          staging_available: r.staging_available,
          emergency: r.emergency,
        })),
      }));
    } catch {
      /* journals are observational; stream stays authoritative */
    }
  };

  const startLiveRunFn = async (inputText: string) => {
    if (stateRef.current.run.run_id && !stateRef.current.run.run_id.startsWith("run-fixture")) {
      retireRunId(stateRef.current.run.run_id);
    }
    if (currentRunIdRef.current && !currentRunIdRef.current.startsWith("run-fixture")) {
      retireRunId(currentRunIdRef.current);
    }
    currentRunIdRef.current = "";
    isStoppedRef.current = false;
    setState((s) => ({
      ...s,
      lastSubmittedText: inputText,
      inputOutcome: null,
      inputErrorCode: null,
    }));
    if (!live.available) {
      setLiveError("connection_unavailable");
      setState((s) => ({
        ...s,
        run: {
          ...IDLE_RUN,
          current_stage: "input",
          stopped: true,
          stop_reason: "Backend connection is unavailable",
          status: "Backend not connected",
        },
        inputOutcome: "ERROR",
        inputErrorCode: "connection_unavailable",
      }));
      return;
    }
    setLiveError(null);
    setLiveApproval(null);
    setPendingSyncApproval(null);
    setState((s) => ({
      ...s,
      runEvents: [],
      inputOutcome: null,
      inputErrorCode: null,
      run: {
        ...s.run,
        run_id: "",
        current_stage: "input",
        status: "submitted — validating input",
        stopped: false,
        stop_reason: "",
        review_state: "awaiting_review",
        error: null,
      },
    }));
    try {
      const res = await live.startRun({
        input_text: inputText,
        source_mode: state.sourceMode === "vault" ? "personal_vault" : "examples",
        retrieval_mode: state.retrievalType,
        app_language: state.appLanguage,
        staging_language: state.stagingLanguage,
        audit_language: state.auditLanguage,
        source_type: "chat_input",
      });
      setState((s) => {
        const matchingEvents = s.runEvents.filter((ev) => !ev.run_id || ev.run_id === res.run_id);
        const alreadyAdvanced = s.run.run_id === res.run_id && s.run.current_stage !== "input";
        return {
          ...s,
          runEvents: matchingEvents,
          run: {
            ...s.run,
            run_id: res.run_id,
            current_stage: alreadyAdvanced ? s.run.current_stage : "input",
            status: alreadyAdvanced
              ? s.run.status
              : (
                "submitted — validating input (retrieval: " +
                (res.retrieval_mode ?? state.retrievalType) +
                ")"
              ),
            stopped: s.run.run_id === res.run_id ? s.run.stopped : false,
            stop_reason: s.run.run_id === res.run_id ? s.run.stop_reason : "",
            review_state: alreadyAdvanced ? s.run.review_state : "awaiting_review",
            error: null,
          },
        };
      });
    } catch (e) {
      const errMsg = (e as Error)?.message ?? "backend_error";
      setLiveError(errMsg);
      setState((s) => ({
        ...s,
        inputOutcome: "REJECTED",
        inputErrorCode: "validation_error",
        inputRecoveryOptions: ["fix_input", "stop"],
        run: {
          ...s.run,
          stopped: true,
          stop_reason: errMsg,
          status: errMsg,
        },
      }));
    }
  };

  const value = useMemo<Store>(
    () => ({
      state,
      setTheme: (theme) => {
        // Theme only; never secrets/paths.
        try {
          localStorage.setItem(THEME_KEY, theme);
        } catch {
          /* ignore */
        }
        setState((s) => ({ ...s, theme }));
      },
      setAppLanguage: (appLanguage) => {
        setState((s) => ({ ...s, appLanguage }));
        if (live.available) {
          live.updateConfig({ app_language: appLanguage })
            .then(() => setLiveError(null))
            .catch((e) => setLiveError((e as Error)?.message || "connection_unavailable"));
        }
      },
      setStagingLanguage: (stagingLanguage) => {
        setState((s) => ({ ...s, stagingLanguage }));
        if (live.available) {
          live.updateConfig({ staging_language: stagingLanguage })
            .then(() => setLiveError(null))
            .catch((e) => setLiveError((e as Error)?.message || "connection_unavailable"));
        }
      },
      setAuditLanguage: (auditLanguage) => {
        setState((s) => ({ ...s, auditLanguage }));
        if (live.available) {
          live.updateConfig({ audit_language: auditLanguage })
            .then(() => setLiveError(null))
            .catch((e) => setLiveError((e as Error)?.message || "connection_unavailable"));
        }
      },
      setSourceMode: (sourceMode) => {
        setState((s) => ({ ...s, sourceMode }));
        if (live.available) {
          live.updateConfig({ source_mode: sourceMode === "vault" ? "personal_vault" : "examples" })
            .then(() => setLiveError(null))
            .catch((e) => setLiveError((e as Error)?.message || "connection_unavailable"));
        }
      },
      setRetrievalType: (retrievalType) => {
        setState((s) => ({ ...s, retrievalType }));
        if (live.available) {
          live.updateConfig({ retrieval_mode: retrievalType })
            .then(() => setLiveError(null))
            .catch((e) => setLiveError((e as Error)?.message || "connection_unavailable"));
        }
      },
      setLlmMode: (llmMode) => {
        setState((s) => ({ ...s, llmMode }));
        if (live.available) {
          live.updateConfig({ llm: { provider: llmMode === "local" ? "llama_cpp" : "disabled" } })
            .then(() => setLiveError(null))
            .catch((e) => setLiveError((e as Error)?.message || "connection_unavailable"));
        }
      },
      // Mock-only transitions, clearly prefixed.
      mockApprove: () =>
        setState((s) => ({ ...s, run: { ...s.run, review_state: "mock_approved" } })),
      mockReject: () =>
        setState((s) => ({ ...s, run: { ...s.run, review_state: "mock_rejected" } })),
      mockEdit: (body) =>
        setState((s) => ({
          ...s,
          run: {
            ...s.run,
            edited_body: body,
            is_executable: s.run.proposal_action === "CREATE",
            review_state: "mock_edited_stale",
          },
        })),
      setComposer: (open, text) =>
        setState((s) => ({ ...s, composerOpen: open, composerText: text ?? s.composerText })),
      rotateJournal: () =>
        setState((s) => ({
          ...s,
          journalIndex: (s.journalIndex + 1) % Math.max(1, s.recentAudits.length),
        })),
      stepJournal: (delta) =>
        setState((s) => {
          const n = Math.max(1, s.recentAudits.length);
          return { ...s, journalIndex: (s.journalIndex + delta + n) % n };
        }),
      setWelcomed: () => {
        try {
          localStorage.setItem(WELCOME_KEY, "1");
        } catch {
          /* ignore */
        }
        setState((s) => ({ ...s, welcomed: true }));
      },
      // §§15–21: mock safe-stop only; Fix keeps SAME run_id (resume), Stop ends it.
      mockSafeStop: (stage, reason, fixTarget) =>
        setState((s) => ({
          ...s,
          run: { ...s.run, stopped: true, stop_reason: reason },
          safeStop: {
            runId: s.run.run_id,
            stage,
            reason,
            fixLabelKey: "ui.safestop.fix",
            fixTarget,
            userChoice: "undecided",
            resumed: false,
          },
        })),
      mockFix: () =>
        setState((s) =>
          s.safeStop === null
            ? s
            : {
                ...s,
                run: { ...s.run, stopped: false, stop_reason: "" },
                safeStop: { ...s.safeStop, userChoice: "fix", resumed: true },
              },
        ),
      mockStopProcess: () =>
        setState((s) =>
          s.safeStop === null
            ? s
            : {
                ...s,
                safeStop: { ...s.safeStop, userChoice: "stop", resumed: false },
              },
        ),
      mockSubmitInput: (text: string) => {
        // Truth-in-representation: never synthesize fake proposals or mock events.
        // Delegate directly to authoritative backend execution.
        const trimmed = text.trim();
        if (!trimmed) return;
        void startLiveRunFn(trimmed);
      },
      mockOnboardingChoice: (key, value) =>
        setState((s) => ({ ...s, onboarding: { ...s.onboarding, [key]: value } })),
      mockDismissOnboarding: (key) =>
        setState((s) => ({ ...s, onboarding: { ...s.onboarding, [key]: true } })),
      mockEngineChoice: (status, selected) =>
        setState((s) => ({
          ...s,
          vectorEngine: { ...s.vectorEngine, status, selected: selected ?? s.vectorEngine.selected },
          vectorIndex: {
            ...s.vectorIndex,
            status: status === "ready" ? "configured_empty" : status === "alternate_configured" ? "configured_empty" : "engine_unavailable",
            engine: selected ?? s.vectorEngine.selected,
          },
        })),
      // Live launcher wiring (Increment 1); fixtures stay for demo/tests.
      liveAvailable: live.available,
      liveConnected,
      liveError,
      liveApproval,
      liveConsentRequest,
      startLiveRun: startLiveRunFn,
      fixInput: () => {
        if (stateRef.current.run.run_id && !stateRef.current.run.run_id.startsWith("run-fixture")) {
          retireRunId(stateRef.current.run.run_id);
        }
        if (currentRunIdRef.current && !currentRunIdRef.current.startsWith("run-fixture")) {
          retireRunId(currentRunIdRef.current);
        }
        currentRunIdRef.current = "";
        isStoppedRef.current = false;
        setPendingSyncApproval(null);
        setState((s) => ({
          ...s,
          composerOpen: true,
          composerText: s.lastSubmittedText || s.composerText,
          inputOutcome: null,
          inputErrorCode: null,
          safeStop: null,
          run: {
            ...IDLE_RUN,
            run_id: "",
            current_stage: "",
            stopped: false,
            stop_reason: "",
            status: "",
          },
        }));
      },
      retryLastInput: async () => {
        const textToRetry = state.lastSubmittedText;
        if (!textToRetry) return;
        if (stateRef.current.run.run_id && !stateRef.current.run.run_id.startsWith("run-fixture")) {
          retireRunId(stateRef.current.run.run_id);
        }
        if (currentRunIdRef.current && !currentRunIdRef.current.startsWith("run-fixture")) {
          retireRunId(currentRunIdRef.current);
        }
        currentRunIdRef.current = "";
        isStoppedRef.current = false;
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        setLiveError(null);
        setLiveApproval(null);
        setPendingSyncApproval(null);
        setState((s) => ({
          ...s,
          runEvents: [],
          inputOutcome: null,
          inputErrorCode: null,
          run: {
            ...s.run,
            run_id: "",
            current_stage: "input",
            status: "submitted retry — validating input",
            stopped: false,
            stop_reason: "",
            review_state: "awaiting_review",
            error: null,
          },
        }));
        try {
          const res = await live.startRun({
            input_text: textToRetry,
            source_mode: state.sourceMode === "vault" ? "personal_vault" : "examples",
            retrieval_mode: state.retrievalType,
            app_language: state.appLanguage,
            staging_language: state.stagingLanguage,
            audit_language: state.auditLanguage,
            source_type: "chat_input",
          });
          setState((s) => {
            const matchingEvents = s.runEvents.filter((ev) => !ev.run_id || ev.run_id === res.run_id);
            const alreadyAdvanced = s.run.run_id === res.run_id && s.run.current_stage !== "input";
            return {
              ...s,
              runEvents: matchingEvents,
              inputOutcome: null,
              inputErrorCode: null,
              run: {
                ...s.run,
                run_id: res.run_id,
                current_stage: alreadyAdvanced ? s.run.current_stage : "input",
                status: alreadyAdvanced ? s.run.status : "submitted retry — validating input",
                stopped: s.run.run_id === res.run_id ? s.run.stopped : false,
                stop_reason: s.run.run_id === res.run_id ? s.run.stop_reason : "",
                review_state: alreadyAdvanced ? s.run.review_state : "awaiting_review",
                error: null,
              },
            };
          });
        } catch (e) {
          const errMsg = (e as Error)?.message ?? "backend_error";
          setLiveError(errMsg);
        }
      },
      dismissRunOutcome: () => {
        if (stateRef.current.run.run_id && !stateRef.current.run.run_id.startsWith("run-fixture")) {
          retireRunId(stateRef.current.run.run_id);
        }
        if (currentRunIdRef.current && !currentRunIdRef.current.startsWith("run-fixture")) {
          retireRunId(currentRunIdRef.current);
        }
        currentRunIdRef.current = "";
        isStoppedRef.current = false;
        setLiveError(null);
        setPendingSyncApproval(null);
        setState((s) => ({
          ...s,
          runEvents: [],
          inputOutcome: null,
          inputErrorCode: null,
          run: {
            ...IDLE_RUN,
            run_id: "",
          },
        }));
      },
      cancelActiveRun: async () => {
        isStoppedRef.current = true;
        setPendingSyncApproval(null);
        if (live.available && state.run.run_id) {
          try {
            await live.cancel({ run_id: state.run.run_id });
          } catch {
            /* ignore */
          }
        }
        setState((s) => ({
          ...s,
          run: {
            ...s.run,
            stopped: true,
            stop_reason: "Operation cancelled by user",
            status: "stopped by user",
          },
          inputOutcome: "STOPPED",
        }));
      },
      liveApprove: async (decision) => {
        if (!live.available || !liveApproval) return;
        if (!state.run.run_id) {
          setLiveError("Missing run ID for approval");
          return;
        }
        setLiveError(null);
        setActionInFlight(true);
        try {
          await live.approve({
            proposal_hash: liveApproval.proposal_hash,
            query_unit_id: liveApproval.query_unit_id,
            decision,
            run_id: state.run.run_id,
            proposal_action: liveApproval.proposal_action,
          });
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              review_state:
                decision === "approve" ? "live_approved" : "live_rejected",
              status: decision === "approve" ? "proposal approved" : "proposal rejected",
            },
          }));
          setLiveApproval(null);
          void refreshJournalsQuiet();
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        } finally {
          setActionInFlight(false);
        }
      },
      liveEditProposal: async (title, body, reason) => {
        if (!live.available || !liveApproval) return;
        setLiveError(null);
        setActionInFlight(true);
        try {
          const res = await live.editProposal({
            title,
            body,
            reason,
            proposal_hash: liveApproval.proposal_hash,
          });
          // The backend returns the authoritative edited proposal with a
          // recomputed hash; the UI reflects it and stays in review state,
          // requiring a fresh explicit approval before any execution.
          setLiveApproval({
            proposal_hash: res.proposal_hash,
            query_unit_id: res.query_unit_id,
            proposal_action: res.proposal_action,
            title: res.title,
            body: res.body,
            confidence_level: res.confidence_level,
            confidence_score: liveApproval.confidence_score,
            confidence_score_band: liveApproval.confidence_score_band,
            classification_type: liveApproval.classification_type,
            destination: liveApproval.destination,
            reason: reason || liveApproval.reason,
            evidence: liveApproval.evidence,
            provenance: liveApproval.provenance,
            uncertainties: liveApproval.uncertainties,
            is_executable: res.proposal_action === "CREATE",
          });
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              proposal_action: res.proposal_action,
              proposal_title: res.title,
              proposal_body: res.body,
              edited_body: res.body,
              proposal_hash: res.proposal_hash,
              review_state: "live_awaiting_review",
              is_executable: res.proposal_action === "CREATE",
              status: res.proposal_action === "CREATE"
                ? "proposal edited — fresh approval required"
                : "proposal edited — acknowledgment required",
            },
          }));
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        } finally {
          setActionInFlight(false);
        }
      },
      liveAccept: async () => {
        if (!live.available) return;
        const proposalHash = liveApproval?.proposal_hash || state.run.proposal_hash;
        if (!proposalHash) {
          setLiveError("Missing proposal hash for acknowledgment");
          return;
        }
        if (!state.run.run_id) {
          setLiveError("Missing run ID for acknowledgment");
          return;
        }
        setLiveError(null);
        setActionInFlight(true);
        try {
          await live.acceptProposal({
            proposal_hash: proposalHash,
            run_id: state.run.run_id,
            proposal_action: liveApproval?.proposal_action || state.run.proposal_action,
          });
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              accepted: true,
              status: "proposal acknowledged",
              review_state: "live_acknowledged",
            },
          }));
          setLiveApproval(null);
          void refreshJournalsQuiet();
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        } finally {
          setActionInFlight(false);
        }
      },
      liveFix: async (inputText) => {
        if (!live.available) return;
        setLiveError(null);
        try {
          const res = await live.fixPausedRun({
            run_id: state.run.run_id,
            input_text: inputText ?? state.composerText,
            source_mode: state.sourceMode === "vault" ? "personal_vault" : "examples",
            retrieval_mode: state.retrievalType,
          });
          setState((s) => ({
            ...s,
            runEvents: [],
            run: {
              ...s.run,
              run_id: res.run_id,
              current_stage: "ingestion",
              status: "resumed with corrected parameters",
              stopped: false,
              stop_reason: "",
              error: null,
            },
          }));
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
      },
      liveStop: async () => {
        if (!live.available) return;
        setLiveError(null);
        try {
          await live.stopPausedRun(state.run.run_id);
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              stopped: true,
              status: "run stopped cleanly by user",
              stop_reason: "stopped by user",
            },
          }));
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
      },
      liveTranslate: async (text, target_lang, source_lang, source_kind, source_id) => {
        const fallbackTarget = target_lang ?? (state.appLanguage === "fa" ? "fa" : "en");
        if (!live.available) {
          return {
            translated_text: text,
            target_lang: fallbackTarget,
            target_language: fallbackTarget,
            source_language: source_lang,
            source_text: text,
            source_kind: source_kind ?? "dynamic_text",
            source_id: source_id ?? "",
            status: "unavailable",
            translator: "disconnected",
            persisted: false,
          };
        }
        try {
          const res = await live.translate({
            text,
            target_lang: fallbackTarget,
            source_lang,
            source_kind: source_kind ?? "dynamic_text",
            source_id: source_id ?? "",
          });
          return res;
        } catch {
          return {
            translated_text: text,
            target_lang: fallbackTarget,
            target_language: fallbackTarget,
            source_language: source_lang,
            source_text: text,
            source_kind: source_kind ?? "dynamic_text",
            source_id: source_id ?? "",
            status: "unavailable",
            translator: "error",
            persisted: false,
          };
        }
      },
      liveIndexVault: async () => {
        if (!live.available) return { indexed: false, reason: "Live client unavailable" };
        try {
          const res = await live.indexVault();
          return res;
        } catch (e) {
          return { indexed: false, reason: (e as Error)?.message ?? "Indexing failed" };
        }
      },
      liveAuthorizeVaultSync: async (vaultRoot?: string) => {
        if (!live.available) return { indexed: false, reason: "Live client unavailable" };
        try {
          const res = await live.authorizeVaultSync(vaultRoot);
          return res;
        } catch (e) {
          return { indexed: false, reason: (e as Error)?.message ?? "Authorization failed" };
        }
      },
      liveRevokeVaultSync: async () => {
        if (!live.available) return { authorized: false, status: "unauthorized" };
        try {
          return await live.revokeVaultSync();
        } catch {
          return { authorized: false, status: "error" };
        }
      },
      liveReconcileVaultSync: async () => {
        if (!live.available) return null;
        try {
          return await live.reconcileVaultSync();
        } catch {
          return null;
        }
      },
      liveGetVaultSyncStatus: async () => {
        if (!live.available) return null;
        try {
          return await live.getVaultSyncStatus();
        } catch {
          return null;
        }
      },
      liveRevealFolder: async (purpose) => {
        if (!live.available) return [];
        try {
          const res = await live.revealFolder(purpose);
          return res.items || [];
        } catch {
          return [];
        }
      },
      liveConsent: async (decision) => {
        if (!live.available || !liveConsentRequest) return;
        setLiveError(null);
        try {
          await live.consent({
            consent_id: liveConsentRequest.consent_id,
            decision,
          });
          setLiveConsentRequest(null);
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
      },
      liveDecideVaultSync: async (decision: "approve" | "reject") => {
        if (!live.available || !pendingSyncApproval) return;
        setLiveError(null);
        try {
          await live.decideVaultSync({
            run_id: pendingSyncApproval.run_id,
            vault_id: pendingSyncApproval.vault_id,
            decision,
          });
          setPendingSyncApproval(null);
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              status:
                decision === "approve"
                  ? "synchronizing Obsidian Vault..."
                  : "synchronization rejected — halting run safely",
            },
          }));
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
      },
      pendingSyncApproval,
      liveCancel: async () => {
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        const runId = state.run.run_id;
        if (!runId) return;
        setLiveError(null);
        setState((s) => ({
          ...s,
          run: { ...s.run,
                 status: "cancellation requested — stopping at next safe boundary" },
        }));
        try {
          const res = await live.cancel({ run_id: runId });
          if (res && res.status === "already_completed") {
            setState((s) => ({
              ...s,
              run: { ...s.run,
                     status: "run already reached a terminal state",
                     stopped: true, stop_reason: "already completed" },
            }));
          }
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
          setState((s) => ({
            ...s,
            run: { ...s.run, status: "cancellation could not be requested" },
          }));
        }
      },
      refreshJournals: async () => {
        await refreshJournalsQuiet();
      },
      pickLiveFolder: async (purpose) => {
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        setLiveError(null);
        let prevRes = state.resources[purpose];
        let prevMode = state.sourceMode;
        setState((s) => {
          prevRes = s.resources[purpose];
          prevMode = s.sourceMode;
          return {
            ...s,
            resources: {
              ...s.resources,
              [purpose]: {
                ...s.resources[purpose],
                validity: "validation_pending",
                reason: "",
              },
            },
          };
        });
        try {
          const res = await live.pickFolder(purpose);
          setState((s) => {
            const wasValid = prevRes.validity === "configured_valid";
            if (res.status === "cancelled") {
              if (wasValid) {
                // User cancelled changing an existing valid folder: preserve existing valid configuration
                return {
                  ...s,
                  sourceMode: prevMode,
                  resources: {
                    ...s.resources,
                    [res.purpose]: {
                      ...prevRes,
                      validity: "configured_valid",
                    },
                  },
                };
              } else {
                // User cancelled initial folder selection: remain not_configured and revert to test mode
                return {
                  ...s,
                  sourceMode: res.purpose === "vault" ? "test" : s.sourceMode,
                  resources: {
                    ...s.resources,
                    [res.purpose]: {
                      ...s.resources[res.purpose],
                      validity: "not_configured",
                      display: "",
                      reason: res.reason || "Selection cancelled",
                    },
                  },
                };
              }
            }

            const isValid = res.status === "configured";
            const isInvalid = res.status === "invalid";
            const newValidity = isValid
              ? "configured_valid"
              : isInvalid
                ? "configured_invalid"
                : "configured_unavailable";

            const nextSourceMode =
              res.purpose === "vault"
                ? (isValid ? "vault" : (wasValid ? prevMode : "test"))
                : s.sourceMode;

            return {
              ...s,
              sourceMode: nextSourceMode,
              resources: {
                ...s.resources,
                [res.purpose]: {
                  ...s.resources[res.purpose],
                  validity: newValidity,
                  display: res.display || (isValid ? "" : (wasValid ? prevRes.display : "")),
                  reason: res.reason,
                },
              },
            };
          });
          void refreshJournalsQuiet();
        } catch (e) {
          const errMsg = (e as Error)?.message ?? "backend_error";
          setLiveError(errMsg);
          setState((s) => {
            const wasValid = prevRes.validity === "configured_valid";
            return {
              ...s,
              sourceMode: wasValid ? prevMode : (purpose === "vault" ? "test" : s.sourceMode),
              resources: {
                ...s.resources,
                [purpose]: {
                  ...s.resources[purpose],
                  validity: wasValid ? "configured_valid" : "validation_error",
                  display: wasValid ? prevRes.display : "",
                  reason: errMsg,
                },
              },
            };
          });
        }
      },
      retryConnection: async () => {
        setLiveError(null);
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        try {
          await live.health();
          setLiveConnected(true);
          const st = await live.state();
          if (st?.instance_id) {
            if (lastInstanceIdRef.current && lastInstanceIdRef.current !== st.instance_id) {
              setPendingSyncApproval(null);
            }
            lastInstanceIdRef.current = st.instance_id;
          }
          if (st?.pending_sync_approval) {
            setPendingSyncApproval(st.pending_sync_approval);
          } else if (!st?.active_run_id) {
            setPendingSyncApproval(null);
          }
          setState((s) => {
            const res = { ...s.resources };
            if (st && st.folders) {
              for (const [purpose, info] of Object.entries(st.folders)) {
                const p = purpose as FolderPurpose;
                if (res[p]) {
                  res[p] = {
                    ...res[p],
                    validity: info.configured ? "configured_valid" : "not_configured",
                    display: info.display,
                  };
                }
              }
            }
            return {
              ...s,
              resources: res,
              sourceMode: st?.modes?.source_mode ? (st.modes.source_mode === "personal_vault" ? "vault" : "test") : s.sourceMode,
              retrievalType: st?.modes?.retrieval_mode ?? s.retrievalType,
              appLanguage: st?.modes?.app_language ?? s.appLanguage,
              stagingLanguage: st?.modes?.staging_language ?? s.stagingLanguage,
              auditLanguage: st?.modes?.audit_language ?? s.auditLanguage,
            };
          });
          void refreshJournalsQuiet();
        } catch (e) {
          setLiveConnected(false);
          setLiveError((e as Error)?.message || "connection_unavailable");
        }
      },
      actionInFlight,
      applyEvent,
      dismissLiveError: () => {
        setLiveError(null);
      },
    }),
    [
      state,
      live,
      liveConnected,
      liveError,
      liveApproval,
      liveConsentRequest,
      pendingSyncApproval,
      actionInFlight,
    ],
  );
  // SSE stream lifecycle: connect once when live is available; abort on unmount.
  useEffect(() => {
    if (!live.available) return;
    const stop = live.streamEvents(
      (event) => applyEvent(event),
      (message) => {
        setLiveConnected(false);
        setLiveError(message);
      },
    );
    void live.health().then(
      async () => {
        setLiveConnected(true);
        void refreshJournalsQuiet();
        try {
          const st = await live.state();
          if (st?.instance_id) {
            if (lastInstanceIdRef.current && lastInstanceIdRef.current !== st.instance_id) {
              setPendingSyncApproval(null);
            }
            lastInstanceIdRef.current = st.instance_id;
          }
          if (st?.pending_sync_approval) {
            setPendingSyncApproval(st.pending_sync_approval);
          } else if (!st?.active_run_id) {
            setPendingSyncApproval(null);
          }
          setState((s) => {
            const res = { ...s.resources };
            if (st && st.folders) {
              for (const [purpose, info] of Object.entries(st.folders)) {
                const p = purpose as FolderPurpose;
                if (res[p]) {
                  res[p] = {
                    ...res[p],
                    validity: info.configured ? "configured_valid" : "not_configured",
                    display: info.display,
                  };
                }
              }
            }
            return {
              ...s,
              resources: res,
              sourceMode: st?.modes?.source_mode ? (st.modes.source_mode === "personal_vault" ? "vault" : "test") : s.sourceMode,
              retrievalType: st?.modes?.retrieval_mode ?? s.retrievalType,
              appLanguage: st?.modes?.app_language ?? s.appLanguage,
              stagingLanguage: st?.modes?.staging_language ?? s.stagingLanguage,
              auditLanguage: st?.modes?.audit_language ?? s.auditLanguage,
              run: st?.paused_run
                ? {
                    ...s.run,
                    run_id: st.paused_run.run_id,
                    stopped: true,
                    stop_reason: st.paused_run.error_message,
                  }
                : st?.active_run_id
                  ? {
                      ...s.run,
                      run_id: st.active_run_id,
                      current_stage: s.run.current_stage || "input",
                      stopped: false,
                    }
                  : !s.run.run_id || s.run.run_id.startsWith("run-fixture")
                    ? { ...IDLE_RUN }
                    : s.run,
            };
          });
        } catch {
          /* bootstrap fallback */
        }
      },
      () => setLiveConnected(false),
    );
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);

  useEffect(() => {
    const timer = setInterval(() => {
      setState((s) => {
        const count = Math.max(1, s.recentAudits.length);
        return {
          ...s,
          journalIndex: (s.journalIndex + 1) % count,
        };
      });
    }, 5000);
    return () => clearInterval(timer);
  }, []);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const v = useContext(Ctx);
  if (!v) throw new Error("store missing");
  return v;
}
