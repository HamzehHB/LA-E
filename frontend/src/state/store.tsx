import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import type { AppState, ConfigResourceState, CurrentRunView } from "../contracts/ui";
import type { FolderPurpose, RunEvent } from "../contracts/transport";
import { FIXTURE_AUDITS, FIXTURE_RUN, FIXTURE_STAGING } from "../fixtures/demo";
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
  mockOnboardingChoice: (key: "vectorBaselineAccepted", value: boolean | null) => void;
  mockDismissOnboarding: (key: "vectorBaselineDismissed" | "engineDismissed") => void;
  mockEngineChoice: (status: AppState["vectorEngine"]["status"], selected?: string) => void;
  /** Live launcher connection (Increment 1). No-op when unavailable. */
  liveAvailable: boolean;
  liveConnected: boolean;
  liveError: string | null;
  startLiveRun: (inputText: string) => Promise<void>;
  liveApprove: (decision: "approve" | "reject") => Promise<void>;
  liveEditProposal: (title: string, body: string, reason: string) => Promise<void>;
  liveConsent: (decision: "approve" | "reject") => Promise<void>;
  liveCancel: () => Promise<void>;
  refreshJournals: () => Promise<void>;
  /** Open the native folder dialog for one purpose via the launcher. */
  pickLiveFolder: (purpose: FolderPurpose) => Promise<void>;
  /** Pending live payloads surfaced from the SSE stream. */
  liveApproval: {
    proposal_hash: string;
    query_unit_id: string;
    proposal_action: string;
    title: string;
    body: string;
    confidence_level: string;
  } | null;
  liveConsentRequest: { consent_id: string; message: string } | null;
}

const Ctx = createContext<Store | null>(null);

const THEME_KEY = "lae-theme";
const WELCOME_KEY = "lae-welcomed";
const MAX_RUN_EVENTS = 200;

export function StoreProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AppState>(() => {
    try {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "dark" || saved === "light") {
        return { ...initialState, theme: saved };
      }
    } catch {
      /* ignore */
    }
    return initialState;
  });
  // Live client: created once, token stays in module memory (never stored).
  const [live] = useState<LiveClient>(() => createLiveClient());
  const [liveConnected, setLiveConnected] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const [liveApproval, setLiveApproval] = useState<Store["liveApproval"]>(null);
  const [liveConsentRequest, setLiveConsentRequest] = useState<
    Store["liveConsentRequest"]
  >(null);

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
    const stage = typeof event.stage === "string" ? event.stage : "";
    const message = typeof event.message === "string" ? event.message : "";
    const runId = typeof event.run_id === "string" ? event.run_id : "";
    const extra = event as RunEvent & Record<string, unknown>;
    if (event.kind === "status" && message.startsWith("retrieval mode:")) {
      const effective = message.includes("semantic") ? "semantic" : "lexical";
      setState((s) => ({
        ...s,
        retrievalType: effective,
        run: { ...s.run, status: message },
      }));
      return;
    }
    if (event.kind === "human_review_required") {
      const proposalHash = String(extra.proposal_hash ?? "");
      if (proposalHash) {
        setLiveApproval({
          proposal_hash: proposalHash,
          query_unit_id: String(extra.query_unit_id ?? ""),
          proposal_action: String(extra.proposal_action ?? ""),
          title: String(extra.title ?? ""),
          body: String(extra.body ?? ""),
          confidence_level: String(extra.confidence_level ?? ""),
        });
        setState((s) => ({
          ...s,
          run: {
            ...s.run,
            run_id: runId || s.run.run_id,
            proposal_action:
              (String(extra.proposal_action ?? "") as CurrentRunView["proposal_action"]) ||
              s.run.proposal_action,
            classification_type:
              String(extra.classification_type ?? s.run.classification_type),
            proposal_title: String(extra.title ?? s.run.proposal_title),
            proposal_body: String(extra.body ?? s.run.proposal_body),
            proposal_hash: proposalHash,
            review_state: "live_awaiting_review",
            current_stage: "human_review",
            status: "awaiting explicit approval",
            error: null,
          },
        }));
      }
      return;
    }
    if (event.kind === "consent_required") {
      const consentId = String(extra.consent_id ?? "");
      if (consentId) {
        setLiveConsentRequest({ consent_id: consentId, message });
        setState((s) => ({
          ...s,
          run: { ...s.run, review_state: "live_consent_required" },
        }));
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
      setState((s) => ({
        ...s,
        run: {
          ...s.run,
          run_id: runId || s.run.run_id,
          current_stage: stage || s.run.current_stage,
          status: message || s.run.status,
          stopped: false,
        },
      }));
      if (event.kind === "completed" || event.kind === "run_completed") {
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
      setState((s) => ({
        ...s,
        run: {
          ...s.run,
          run_id: runId || s.run.run_id,
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
            a.outcome === "passed" ? "journal.passed" : "journal.failed",
          stagingNoteKey: a.staging_available
            ? "journal.stagingAvailable"
            : "journal.stagingNotCreated",
          // §4 stable identity for cross-journal correlation (never inferred
          // from timestamps/filenames; taken only from the persisted record).
          runId: a.run_id || undefined,
          proposalHash: a.proposal_hash || undefined,
          artifactReference: a.artifact_reference || undefined,
        })),
        recentStaging: data.staging.map((r) => ({
          id: r.record_id,
          timestamp: r.timestamp,
          statusKey: "journal.staged",
          stagingNoteKey: null,
          runId: r.run_id || undefined,
          proposalHash: r.proposal_hash || undefined,
          artifactReference: r.artifact_reference || undefined,
        })),
      }));
    } catch {
      /* journals are observational; stream stays authoritative */
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
      setAppLanguage: (appLanguage) => setState((s) => ({ ...s, appLanguage })),
      setStagingLanguage: (stagingLanguage) =>
        setState((s) => ({ ...s, stagingLanguage })),
      setAuditLanguage: (auditLanguage) =>
        setState((s) => ({ ...s, auditLanguage })),
      setSourceMode: (sourceMode) => setState((s) => ({ ...s, sourceMode })),
      setRetrievalType: (retrievalType) => setState((s) => ({ ...s, retrievalType })),
      setLlmMode: (llmMode) => setState((s) => ({ ...s, llmMode })),
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
      startLiveRun: async (inputText) => {
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        setLiveError(null);
        setLiveApproval(null);
        try {
          const res = await live.startRun({
            input_text: inputText,
            source_mode: state.sourceMode === "vault" ? "personal_vault" : "examples",
            retrieval_mode: state.retrievalType,
            app_language: state.appLanguage,
            staging_language: state.stagingLanguage,
            audit_language: state.auditLanguage,
          });
          setState((s) => ({
            ...s,
            runEvents: [],
            run: {
              ...s.run,
              run_id: res.run_id,
              current_stage: "ingestion",
              status:
                "submitted — awaiting backend events (retrieval: " +
                (res.retrieval_mode ?? state.retrievalType) +
                ")",
              stopped: false,
              stop_reason: "",
              review_state: "awaiting_review",
              error: null,
            },
          }));
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
        }
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
      liveCancel: async () => {
        if (!live.available) {
          setLiveError("connection_unavailable");
          return;
        }
        const runId = state.run.run_id;
        if (!runId) return;
        setLiveError(null);
        // Cooperative cancellation is confirmed by the backend; do NOT mark
        // the run stopped here — the real terminal state arrives via a run
        // event (stopped / run_completed / run_rejected) and the audit trail.
        setState((s) => ({
          ...s,
          run: { ...s.run,
                 status: "cancellation requested — stopping at next safe boundary" },
        }));
        try {
          const res = await live.cancel({ run_id: runId });
          if (res && res.status === "already_completed") {
            // Honest: the run already reached a terminal state; do not
            // pretend a cancellation took effect.
            setState((s) => ({
              ...s,
              run: { ...s.run,
                     status: "run already reached a terminal state",
                     stopped: true, stop_reason: "already completed" },
            }));
          }
        } catch (e) {
          setLiveError((e as Error)?.message ?? "backend_error");
          // Revert the optimistic status so the UI does not mislead.
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
    }),
    [
      state,
      live,
      liveConnected,
      liveError,
      liveApproval,
      liveConsentRequest,
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
      () => {
        setLiveConnected(true);
        void refreshJournalsQuiet();
      },
      () => setLiveConnected(false),
    );
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const v = useContext(Ctx);
  if (!v) throw new Error("store missing");
  return v;
}
