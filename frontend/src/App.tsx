import { useEffect } from "react";
import { StoreProvider, useStore } from "./state/store";
import { TopNav } from "./components/TopNav";
import { CurrentAudit, Workflow } from "./components/Workflow";
import { Composer, Journals, ProcessingResult, Welcome } from "./components/Panels";
import { translate } from "./i18n/resources";

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
  const { state, liveConsentRequest, liveConsent, liveError } = useStore();
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
      {liveError && <p role="alert" className="err-banner">{liveError}</p>}

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

      {/* Bottom Composer Dock + Centered Responsibility Disclaimer */}
      <footer className="composer-dock">
        <Composer />
        {!state.composerOpen && (
          <p className="responsibility">{translate(state.appLanguage, "ui.responsibility")}</p>
        )}
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
