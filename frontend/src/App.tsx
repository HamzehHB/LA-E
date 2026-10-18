import { useEffect } from "react";
import { StoreProvider, useStore } from "./state/store";
import { TopNav } from "./components/TopNav";
import { CurrentAudit, Workflow } from "./components/Workflow";
import { Composer, Journals, ProcessingResult, Welcome } from "./components/Panels";
import type { FolderPurpose } from "./contracts/transport";

function formatLiveError(err: string, lang: string): string {
  if (err.includes("connection_unavailable") || err.includes("Failed to fetch") || err.includes("NetworkError")) {
    return lang === "fa"
      ? "ارتباط با پس‌اند برقرار نیست. لطفاً از اجرای لانچر مطمئن شوید."
      : "Backend connection is unavailable. Ensure the launcher is running.";
  }
  if (err.includes("authentication_error")) {
    return lang === "fa"
      ? "نشست امنیتی منقضی شده است. لطفاً برنامه را مجدداً از لانچر باز کنید."
      : "Session authentication expired. Please relaunch the application.";
  }
  if (err.includes("validation_error")) {
    return lang === "fa"
      ? `خطای اعتبارسنجی: ${err}`
      : `Validation error: ${err}`;
  }
  return err;
}

function isConnectionError(err: string): boolean {
  return (
    err.includes("connection_unavailable") ||
    err.includes("Failed to fetch") ||
    err.includes("NetworkError") ||
    err.includes("reconnecting")
  );
}

function isFolderError(err: string): boolean {
  const lower = err.toLowerCase();
  return (
    lower.includes("vault") ||
    lower.includes("staging") ||
    lower.includes("audit") ||
    lower.includes("vector")
  );
}

function folderPurposeFromError(err: string): FolderPurpose {
  const lower = err.toLowerCase();
  if (lower.includes("staging")) return "staging";
  if (lower.includes("audit")) return "audit";
  if (lower.includes("vector") || lower.includes("db") || lower.includes("database")) return "vector_db";
  return "vault";
}

/**
 * Exactly 13 decorative stars connected by a subtle constellation web:
 * 3 elevated top stars (y: 14%-22%) and 10 lower stars (y: 56%-72%).
 * Aria-hidden, purely ornamental.
 */
const STAR_POSITIONS: { x: number; y: number; s: number }[] = [
  { x: 5, y: 62, s: 0.9 },
  { x: 14, y: 18, s: 0.9 }, // Top 1
  { x: 22, y: 68, s: 1.1 },
  { x: 30, y: 56, s: 0.85 },
  { x: 38, y: 70, s: 1.0 },
  { x: 45, y: 60, s: 0.9 },
  { x: 50, y: 14, s: 1.05 }, // Top 2
  { x: 58, y: 66, s: 0.85 },
  { x: 67, y: 58, s: 1.05 },
  { x: 75, y: 70, s: 0.8 },
  { x: 84, y: 20, s: 0.95 }, // Top 3
  { x: 91, y: 62, s: 0.95 },
  { x: 97, y: 68, s: 0.85 },
];

function Shell() {
  const {
    state,
    liveConsentRequest,
    liveConsent,
    liveError,
    retryConnection,
    dismissLiveError,
    pickLiveFolder,
  } = useStore();
  const rtl = state.appLanguage === "fa";

  useEffect(() => {
    document.documentElement.dataset.theme = state.theme;
    document.documentElement.dir = rtl ? "rtl" : "ltr";
    document.documentElement.lang = state.appLanguage;
  }, [state.theme, rtl, state.appLanguage]);

  return (
    <div className="app">
      <Welcome />

      {/* Top Header Bar: Brand + 3 Tabs + 3 Dropdowns */}
      <header className="top-header">
        <TopNav />
      </header>

      {/* Cloud Consent Prompt (if requested) */}
      {liveConsentRequest && (
        <div role="alert" className="consent-banner">
          <p>{liveConsentRequest.message} ({liveConsentRequest.consent_id})</p>
          <div className="review">
            <button type="button" className="btn approve" onClick={() => void liveConsent("approve")}>Approve once</button>
            <button type="button" className="btn reject" onClick={() => void liveConsent("reject")}>Reject</button>
          </div>
        </div>
      )}
      {/* Actionable Error Banner with Remediation Affordance */}
      {liveError && (
        <div role="alert" className="app-error-banner">
          <div className="error-message">
            <span className="error-icon" aria-hidden="true">⚠️</span>
            <span>{formatLiveError(liveError, state.appLanguage)}</span>
          </div>
          <div className="error-actions">
            {isConnectionError(liveError) && (
              <button
                type="button"
                className="btn small approve"
                onClick={() => void retryConnection()}
              >
                {state.appLanguage === "fa" ? "تلاش مجدد برای اتصال" : "Retry Connection"}
              </button>
            )}
            {isFolderError(liveError) && (
              <button
                type="button"
                className="btn small approve"
                onClick={() => void pickLiveFolder(folderPurposeFromError(liveError))}
              >
                {state.appLanguage === "fa" ? "انتخاب و تصحیح پوشه" : "Configure Folder"}
              </button>
            )}
            <button
              type="button"
              className="btn small ghost dismiss-btn"
              onClick={dismissLiveError}
              aria-label="Dismiss error"
            >
              {state.appLanguage === "fa" ? "بستن" : "Dismiss"}
            </button>
          </div>
        </div>
      )}

      {/* Upper Band: 13-Star Constellation & DeepThink Current Audit Overlay */}
      <div className="constellation-stage">
        <div className="stars" aria-hidden="true">
          <svg className="star-web" viewBox="0 0 100 100" preserveAspectRatio="none" focusable="false">
            <polyline points={STAR_POSITIONS.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" />
          </svg>
          {STAR_POSITIONS.map((pos, i) => (
            <span
              key={i}
              className="star"
              style={{
                left: `${pos.x}%`,
                top: `${pos.y}%`,
                ["--star-scale" as string]: String(pos.s),
              }}
            >
              ✦
            </span>
          ))}
        </div>
        <CurrentAudit />
      </div>

      {/* Lower Dashboard Workspace: Left Workflow | Center Processing Result | Right Stacked Journals */}
      <main className="dashboard">
        <div className="workflow-col">
          <Workflow />
        </div>
        <div className="workspace-center">
          <ProcessingResult />
        </div>
        <aside className="sidebar-right">
          <Journals />
        </aside>
      </main>

      {/* Bottom Composer Dock */}
      <footer className="composer-dock">
        <Composer />
      </footer>
    </div>
  );
}

export function App() {
  return (
    <StoreProvider>
      <Shell />
    </StoreProvider>
  );
}
