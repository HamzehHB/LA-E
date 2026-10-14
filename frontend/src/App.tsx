import { useEffect, useState } from "react";
import { StoreProvider, useStore } from "./state/store";
import { TopNav } from "./components/TopNav";
import { CurrentAudit, Workflow } from "./components/Workflow";
import { Composer, Journals, ProcessingResult, SettingsPanel, StatusStrip, Welcome } from "./components/Panels";
import { translate } from "./i18n/resources";

/**
 * Exactly 13 decorative stars (reference image). Purely ornamental:
 * aria-hidden, no state meaning. Positioned across the band; yellow in
 * light mode, blue in dark mode (see styles.css --star color).
 */
const STAR_POSITIONS: { x: number; y: number; s: number }[] = [
  { x: 4, y: 60, s: 1.0 },
  { x: 12, y: 20, s: 0.85 },
  { x: 20, y: 68, s: 1.15 },
  { x: 28, y: 56, s: 0.7 },
  { x: 36, y: 72, s: 1.05 },
  { x: 44, y: 62, s: 0.9 },
  { x: 50, y: 14, s: 1.0 },
  { x: 58, y: 66, s: 0.8 },
  { x: 66, y: 58, s: 1.1 },
  { x: 74, y: 70, s: 0.75 },
  { x: 82, y: 22, s: 0.95 },
  { x: 90, y: 60, s: 1.0 },
  { x: 97, y: 68, s: 0.85 },
];

function Shell() {
  const { state, rotateJournal, liveAvailable, liveConnected, liveConsentRequest, liveConsent, liveError } = useStore();
  const rtl = state.appLanguage === "fa";
  const [settingsOpen, setSettingsOpen] = useState(true);

  useEffect(() => {
    document.documentElement.dataset.theme = state.theme;
    document.documentElement.dir = rtl ? "rtl" : "ltr";
    document.documentElement.lang = state.appLanguage;
  }, [state.theme, rtl, state.appLanguage]);

  // Fixture journal rotation: cancellable interval, cleaned up (Invariant: no leaks).
  useEffect(() => {
    const id = window.setInterval(() => rotateJournal(), 5000);
    return () => window.clearInterval(id);
  }, [rotateJournal]);

  return (
    <div className="app">
      <Welcome />
      <header>
        <div className="brand-row">
          <div className="brand-cluster">
            <div className="brand">
              <span className="logo" aria-label="LA-E">
                <span className="logo-l" aria-hidden="true">L</span><span className="logo-a" aria-hidden="true">A</span><span className="logo-sep" aria-hidden="true">-</span><span className="logo-e" aria-hidden="true">E</span>
              </span>
              <span className="ver">v0.1</span>
            </div>
            <button
              type="button"
              className="gear"
              aria-label={translate(state.appLanguage, "ui.nav.settings")}
              aria-pressed={settingsOpen}
              aria-expanded={settingsOpen}
              onClick={() => setSettingsOpen((v) => !v)}
            >
              ⚙
            </button>
            {settingsOpen && <SettingsPanel />}
          </div>
          <p className="live-line" aria-live="polite">
            <span className={`live-dot ${liveAvailable ? (liveConnected ? "on" : "off") : ""}`} aria-hidden="true" />
            {liveAvailable
              ? translate(state.appLanguage, "ui.live.connected")
              : translate(state.appLanguage, "ui.live.demo")}
            {liveAvailable && !liveConnected ? " · reconnecting…" : ""}
          </p>
        </div>
        {liveConsentRequest && (
          <div role="alert" className="panel">
            <p>{liveConsentRequest.message} ({liveConsentRequest.consent_id})</p>
            <div className="review">
              <button type="button" className="btn approve" onClick={() => void liveConsent("approve")}>Approve once</button>
              <button type="button" className="btn reject" onClick={() => void liveConsent("reject")}>Reject</button>
            </div>
          </div>
        )}
        {liveError && <p role="alert" className="panel err-text">{liveError}</p>}
        <TopNav />
      </header>
      <div className="constellation-stage">
        <div className="stars" aria-hidden="true">
          <svg className="star-web" viewBox="0 0 100 100" preserveAspectRatio="none" focusable="false">
            <polyline points={STAR_POSITIONS.map((p) => `${p.x},${p.y}`).join(" ")} fill="none" />
          </svg>
          {STAR_POSITIONS.map((pos, i) => (
            <span key={i} className="star" style={{ left: `${pos.x}%`, top: `${pos.y}%`, ["--star-scale" as string]: String(pos.s) }}>✦</span>
          ))}
        </div>
        <CurrentAudit />
      </div>
      <main className="dashboard">
        <div className="workspace">
          <Workflow />
          <ProcessingResult />
        </div>
        <aside className="sidebar">
          <Journals />
        </aside>
      </main>
      <StatusStrip />
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
