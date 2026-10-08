import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import type { AppState, ConfigResourceState } from "../contracts/ui";
import type { FolderPurpose } from "../contracts/transport";
import { FIXTURE_AUDITS, FIXTURE_RUN, FIXTURE_STAGING } from "../fixtures/demo";


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
  recentAudits: [...FIXTURE_AUDITS],
  recentStaging: [...FIXTURE_STAGING],
  composerOpen: false,
  composerText: "",
  journalIndex: 0,
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
  setSourceMode: (m: AppState["sourceMode"]) => void;
  setRetrievalType: (r: AppState["retrievalType"]) => void;
  setLlmMode: (m: AppState["llmMode"]) => void;
  mockApprove: () => void;
  mockReject: () => void;
  mockEdit: (body: string) => void;
  setComposer: (open: boolean, text?: string) => void;
  rotateJournal: () => void;
  mockSafeStop: (stage: string, reason: string, fixTarget: string) => void;
  mockFix: () => void;
  mockStopProcess: () => void;
  mockOnboardingChoice: (key: "vectorBaselineAccepted", value: boolean | null) => void;
  mockDismissOnboarding: (key: "vectorBaselineDismissed" | "engineDismissed") => void;
  mockEngineChoice: (status: AppState["vectorEngine"]["status"], selected?: string) => void;
}

const Ctx = createContext<Store | null>(null);

const THEME_KEY = "lae-theme";

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
    }),
    [state],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useStore(): Store {
  const v = useContext(Ctx);
  if (!v) throw new Error("store missing");
  return v;
}
