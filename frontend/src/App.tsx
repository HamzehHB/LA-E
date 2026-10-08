import { useEffect } from "react";
import { StoreProvider, useStore } from "./state/store";
import { TopNav } from "./components/TopNav";
import { CurrentAudit, Workflow } from "./components/Workflow";
import { Composer, EnginePanel, Journals, OnboardingPanel, ProcessingResult, ResourceStatusPanel, SafeStopPanel, SettingsPanel } from "./components/Panels";
import { translate } from "./i18n/resources";

function Shell() {
  const { state, rotateJournal } = useStore();
  const rtl = state.appLanguage === "fa";

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
      <header>
        <h1>{translate(state.appLanguage, "ui.appName")}</h1>
        <TopNav />
      </header>
      <div className="stars" aria-hidden="true" />
      {state.composerOpen ? (
        <main>
          <Composer />
        </main>
      ) : (
        <main>
          <div>
            <OnboardingPanel />
            <EnginePanel />
            <CurrentAudit />
            <SafeStopPanel />
            <Workflow />
            <ProcessingResult />
            <ResourceStatusPanel />
            <SettingsPanel />
            <Composer />
          </div>
          <Journals />
        </main>
      )}
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
