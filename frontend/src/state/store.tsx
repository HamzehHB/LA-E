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
    { purpose, validity: purpose === "vault" ? "configured_valid" : "not_configured", display: purpose === "vault" ? "<TEST_VAULT>" : "", reason: "" },
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
  const [liveApproval, setLiveApproval] = useState<Store["liveApproval"]>(null);
  const [liveConsentRequest, setLiveConsentRequest] = useState<
    Store["liveConsentRequest"]
  >(null);
  const [pendingSyncApproval, setPendingSyncApproval] = useState<PendingSyncApprovalInfo | null>(null);
  const lastInstanceIdRef = useRef<string>("");

  const stateRef = useRef(state);
  stateRef.current = state;

  const applyEvent = (event: RunEvent) => {
    setLiveConnected(true);
    // Retain a bounded, run-scoped event log for Current Audit + timeline.
    setState((s) => {
      const rid = typeof event.run_id === "string" ? event.run_id : "";
      const sameRun = !rid || rid === s.run.run_id || s.run.run_id === "";
      if (!sameRun) return s;
      const next = s.runEvents.length >= MAX_RUN_EVENTS
        ? [...s.runEvents.slice(s.runEvents.length - MAX_RUN_EVENTS + 1), event]
        : [...s.runEvents, event];
      return { ...s, runEvents: next };
    });
    const runId = typeof event.run_id === "string" ? event.run_id : "";
    const stage = typeof event.stage === "string" ? event.stage : "";
    const message = typeof event.message === "string" ? event.message : "";
    const extra = event as RunEvent & Record<string, unknown>;

    // Stale event protection: If this run has been stopped or rejected,
    // prevent any delayed SSE events from reviving it to RUNNING or AWAITING_REVIEW.
    if (stateRef.current.run.stopped && (!runId || runId === stateRef.current.run.run_id)) {
      if (
        event.kind !== "stopped" &&
        event.kind !== "run_rejected" &&
        event.kind !== "error"
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
        setLiveApproval({
          proposal_hash: proposalHash,
          query_unit_id: String(extra.query_unit_id ?? ""),
          proposal_action: String(extra.proposal_action ?? ""),
          title: String(extra.title ?? ""),
          body: String(extra.body ?? ""),
          confidence_level: String(extra.confidence_level ?? ""),
          confidence_score: confScore,
          confidence_score_band: confBand,
          classification_type: classType,
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
                (String(extra.proposal_action ?? "") as CurrentRunView["proposal_action"]) ||
                s.run.proposal_action,
              classification_type: classType,
              proposal_title: String(extra.title ?? s.run.proposal_title),
              proposal_body: String(extra.body ?? s.run.proposal_body),
              confidence_score: confScore,
              confidence_score_band: confBand,
              proposal_hash: proposalHash,
              review_state: "live_awaiting_review",
              current_stage: "human_review",
              status: "awaiting explicit approval",
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
        // Prevent stale events from reviving a run that was stopped or rejected
        if (s.run.stopped && (!runId || runId === s.run.run_id)) {
          return s;
        }
        return {
          ...s,
          run: {
            ...s.run,
            run_id: runId || s.run.run_id,
            current_stage: stage || s.run.current_stage,
            status: message || s.run.status,
            stopped: false,
          },
        };
      });
      if (event.kind === "completed" || event.kind === "run_completed") {
        setPendingSyncApproval(null);
        void refreshJournalsQuiet();
      }
      return;
    }
    if (
      event.kind === "run_rejected" ||
      event.kind === "stopped" ||
      event.kind === "error" ||
      event.kind === "warning"
    ) {
      if (event.kind !== "warning") {
        setPendingSyncApproval(null);
      }
      const outcome = (event.outcome as AppState["inputOutcome"]) ||
        (event.kind === "stopped" ? "STOPPED" : "REJECTED");
      setState((s) => ({
        ...s,
        inputOutcome: outcome,
        inputErrorCode: event.error_code ?? null,
        inputRecoveryOptions: event.recovery_options ?? ["fix_input", "stop"],
        run: {
          ...s.run,
          run_id: runId || s.run.run_id,
          current_stage: stage || s.run.current_stage || "input",
          stopped: event.kind !== "warning",
          stop_reason: message,
          status: message || s.run.status,
        },
      }));
      return;
    }
  };

  const refreshJournalsQuiet = async () => {
    if (!live.available) return;
    try {
      const data = await live.journals();
      setState((s) => ({
        ...s,
        recentAudits: data.audits.map((a) => ({
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
        recentStaging: data.staging.map((r) => ({
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
      setState((s) => ({
        ...s,
        runEvents: [],
        run: {
          ...s.run,
          run_id: res.run_id,
          current_stage: "input",
          status:
            "submitted — validating input (retrieval: " +
            (res.retrieval_mode ?? state.retrievalType) +
            ")",
          stopped: false,
          stop_reason: "",
          review_state: "awaiting_review",
          error: null,
        },
      }));
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
          run: { ...s.run, edited_body: body, review_state: "mock_edited_stale" },
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
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        setLiveError(null);
        setLiveApproval(null);
        setPendingSyncApproval(null);
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
          setState((s) => ({
            ...s,
            runEvents: [],
            inputOutcome: null,
            inputErrorCode: null,
            run: {
              ...s.run,
              run_id: res.run_id,
              current_stage: "input",
              status: "submitted retry — validating input",
              stopped: false,
              stop_reason: "",
              review_state: "awaiting_review",
              error: null,
            },
          }));
        } catch (e) {
          const errMsg = (e as Error)?.message ?? "backend_error";
          setLiveError(errMsg);
        }
      },
      dismissRunOutcome: () => {
        setLiveError(null);
        setPendingSyncApproval(null);
        setState((s) => ({
          ...s,
          inputOutcome: null,
          inputErrorCode: null,
          run: {
            ...IDLE_RUN,
            run_id: "",
          },
        }));
      },
      cancelActiveRun: async () => {
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
        setLiveError(null);
        try {
          await live.approve({
            proposal_hash: liveApproval.proposal_hash,
            query_unit_id: liveApproval.query_unit_id,
            decision,
          });
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              review_state:
                decision === "approve" ? "live_approved" : "live_rejected",
            },
          }));
          setLiveApproval(null);
          void refreshJournalsQuiet();
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
      },
      liveEditProposal: async (title, body, reason) => {
        if (!live.available || !liveApproval) return;
        setLiveError(null);
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
              status: "proposal edited — fresh approval required",
            },
          }));
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
      },
      liveAccept: async () => {
        if (!live.available) return;
        setLiveError(null);
        try {
          await live.acceptProposal(state.run.run_id);
          setState((s) => ({
            ...s,
            run: {
              ...s.run,
              accepted: true,
              status: "proposal acknowledged",
              review_state: "live_approved",
            },
          }));
          setLiveApproval(null);
          void refreshJournalsQuiet();
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
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
        setState((s) => ({
          ...s,
          resources: {
            ...s.resources,
            [purpose]: {
              ...s.resources[purpose],
              validity: "validation_pending",
              reason: "",
            },
          },
        }));
        try {
          const res = await live.pickFolder(purpose);
          setState((s) => ({
            ...s,
            resources: {
              ...s.resources,
              [res.purpose]: {
                ...s.resources[res.purpose],
                validity:
                  res.status === "configured"
                    ? "configured_valid"
                    : res.status === "invalid"
                      ? "configured_invalid"
                      : res.status === "cancelled"
                        ? "not_configured"
                        : "configured_unavailable",
                display: res.display,
                reason: res.reason,
              },
            },
          }));
          void refreshJournalsQuiet();
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
          setState((s) => ({
            ...s,
            resources: {
              ...s.resources,
              [purpose]: {
                ...s.resources[purpose],
                validity: "validation_error",
                reason: (e as Error)?.message ?? "backend_error",
              },
            },
          }));
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
              sourceMode: st?.modes?.source_mode === "personal_vault" ? "vault" : "test",
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
              sourceMode: st?.modes?.source_mode === "personal_vault" ? "vault" : "test",
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
