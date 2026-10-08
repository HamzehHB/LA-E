import { useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";

export function ProcessingResult() {
  const { state, mockApprove, mockReject, mockEdit } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(state.run.proposal_body);
  const executable = state.run.proposal_action === "CREATE";

  return (
    <section aria-label={t("ui.result.title")} className="panel result">
      <h2>{t("ui.result.title")}</h2>
      <dl>
        <div><dt>{t("ui.result.class")}</dt><dd>{state.run.proposal_action}</dd></div>
        <div><dt>{t("ui.result.proposalType")}</dt><dd>{state.run.proposal_action}</dd></div>
        <div><dt>{t("ui.result.confidence")}</dt><dd>{state.run.confidence_level} (ordinal, not a score)</dd></div>
      </dl>
      <h3>{t("ui.result.output")}</h3>
      <p className="output">{state.run.edited_body ?? state.run.proposal_body}</p>
      <p className="hash">proposal_hash: {state.run.proposal_hash} (display only; no authoritative re-hash in this foundation)</p>
      {executable ? (
        <div className="review" role="group" aria-label="human review (mock)">
          <p><small>{t("ui.review.mockNote")}</small></p>
          <button type="button" onClick={mockApprove}>{t("ui.review.approve")}</button>
          <button type="button" onClick={mockReject}>{t("ui.review.reject")}</button>
          <button type="button" onClick={() => { setDraft(state.run.edited_body ?? state.run.proposal_body); setEditing((v) => !v); }}>
            {t("ui.review.edit")}
          </button>
          <span aria-live="polite">mock state: {state.run.review_state}</span>
          {editing && (
            <div>
              <label htmlFor="edit">Edit (mock stale)</label>
              <textarea id="edit" value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} />
              <button type="button" onClick={() => { mockEdit(draft); setEditing(false); }}>
                {t("ui.review.saveEdit")}
              </button>
            </div>
          )}
        </div>
      ) : (
        <p role="note">{t("ui.review.nonExecutable")}</p>
      )}
    </section>
  );
}

export function Journals() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const audit = state.recentAudits[state.journalIndex] ?? state.recentAudits[0];
  return (
    <div className="journals">
      <section aria-label={t("ui.journals.audit")} className="panel journal">
        <h2>{t("ui.journals.audit")}</h2>
        {audit ? (
          <article>
            <p><time>{audit.timestamp}</time> · <span>{audit.id}</span></p>
            <p>{t(audit.statusKey as Parameters<typeof translate>[1])}{audit.stagingNoteKey ? ` · ${t(audit.stagingNoteKey as Parameters<typeof translate>[1])}` : ""}</p>
            <p><small>item {state.journalIndex + 1} of {state.recentAudits.length} (rotating fixture)</small></p>
          </article>
        ) : (
          <p>{t("ui.empty.journals")}</p>
        )}
      </section>
      <section aria-label={t("ui.journals.staging")} className="panel journal">
        <h2>{t("ui.journals.staging")}</h2>
        {state.recentStaging.length === 0 ? (
          <p>{t("ui.empty.journals")}</p>
        ) : (
          <ul>
            {state.recentStaging.slice(0, 5).map((r) => (
              <li key={r.id}><span>{r.id}</span> · {t(r.statusKey as Parameters<typeof translate>[1])} · <time>{r.timestamp}</time></li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

export function Composer() {
  const { state, setComposer } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (!state.composerOpen) {
    return (
      <div className="composer-collapsed">
        <button type="button" onClick={() => setComposer(true)}>✎ {t("ui.composer.placeholder")}</button>
      </div>
    );
  }
  return (
    <div className="composer">
      <label htmlFor="composer">{t("ui.composer.placeholder")}</label>
      <textarea
        id="composer"
        rows={6}
        value={state.composerText}
        onChange={(e) => setComposer(true, e.target.value)}
        placeholder={t("ui.composer.placeholder")}
      />
      <div>
        <button type="button">{t("ui.composer.submit")}</button>
        <button type="button" onClick={() => setComposer(false)}>{t("ui.composer.close")}</button>
      </div>
      <p className="responsibility">{t("ui.responsibility")}</p>
    </div>
  );
}

export function SettingsPanel() {
  const { state, setTheme, setAppLanguage } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  return (
    <section aria-label={t("ui.settings.title")} className="panel settings">
      <h2>{t("ui.settings.title")}</h2>
      <div role="radiogroup" aria-label={t("ui.settings.display")}>
        <button type="button" role="radio" aria-checked={state.theme === "light"} onClick={() => setTheme("light")}>{t("ui.settings.light")}</button>
        <button type="button" role="radio" aria-checked={state.theme === "dark"} onClick={() => setTheme("dark")}>{t("ui.settings.dark")}</button>
      </div>
      <div role="radiogroup" aria-label={t("ui.settings.appLanguage")}>
        <button type="button" role="radio" aria-checked={state.appLanguage === "en"} onClick={() => setAppLanguage("en")}>English</button>
        <button type="button" role="radio" aria-checked={state.appLanguage === "fa"} onClick={() => setAppLanguage("fa")}>فارسی</button>
      </div>
    </section>
  );
}


export function OnboardingPanel() {
  const { state, mockOnboardingChoice, mockDismissOnboarding } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (state.onboarding.vectorBaselineDismissed || state.onboarding.vectorBaselineAccepted !== null) return null;
  return (
    <section aria-label="onboarding" className="panel">
      <p>{t("ui.onboarding.vectorBaseline")}</p>
      <button type="button" onClick={() => mockOnboardingChoice("vectorBaselineAccepted", true)}>{t("ui.onboarding.accept")}</button>
      <button type="button" onClick={() => mockDismissOnboarding("vectorBaselineDismissed")}>{t("ui.onboarding.decline")}</button>
    </section>
  );
}

export function EnginePanel() {
  const { state, mockEngineChoice, mockDismissOnboarding } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (state.onboarding.engineDismissed || state.vectorEngine.status === "ready" || state.vectorEngine.status === "alternate_configured") return null;
  return (
    <section aria-label="engine" className="panel">
      <p>{t("ui.engine.installPrompt")}</p>
      <button type="button" onClick={() => mockEngineChoice("ready", "lancedb")}>{t("ui.engine.install")}</button>
      <button type="button" onClick={() => mockEngineChoice("alternate_configured", "other-engine")}>{t("ui.engine.chooseOther")}</button>
      <button type="button" onClick={() => { mockEngineChoice("user_declined"); mockDismissOnboarding("engineDismissed"); }}>{t("ui.onboarding.decline")}</button>
      {state.vectorEngine.status === "user_declined" && <p role="note">{t("ui.engine.declined")}</p>}
    </section>
  );
}

export function ResourceStatusPanel() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  return (
    <section aria-label="resources" className="panel">
      <h2>{t("ui.vector.status")}: {state.vectorIndex.status}</h2>
      <p>{t("ui.model.recommended")}: {t("ui.model.bge")} · {t("ui.model.engineLance")} — {t("ui.llm.localStatus")}: {state.llmAvailability.local} · {t("ui.llm.cloudStatus")}: {state.llmAvailability.cloud}</p>
      <p>{t("ui.audit.layout")}</p>
      <h3>{t("ui.eligibility.title")}</h3>
      <p role="note">{t("ui.eligibility.note")}</p>
      <p role="note">{t("ui.sync.note")}</p>
    </section>
  );
}

export function SafeStopPanel() {
  const { state, mockSafeStop, mockFix, mockStopProcess } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (state.safeStop === null) {
    return (
      <div>
        <button type="button" onClick={() => mockSafeStop(state.run.current_stage, "mock dependency unavailable", "embedding-model")}>
          {t("ui.demo.safeStop")}
        </button>
      </div>
    );
  }
  const s = state.safeStop;
  return (
    <section aria-label={t("ui.safestop.title")} className="panel safestop" role="alert">
      <h2>{t("ui.safestop.title")}</h2>
      <p>run {s.runId} · stage {s.stage} · {s.reason}</p>
      <div className="safestop-actions">
        <button type="button" className="fix" aria-label={t("ui.safestop.fix")} onClick={mockFix}>{t("ui.safestop.fix")}</button>
        <button type="button" className="stop" aria-label={t("ui.safestop.stop")} onClick={mockStopProcess}>{t("ui.safestop.stop")}</button>
      </div>
      {s.userChoice === "fix" && s.resumed && <p aria-live="polite">{t("ui.safestop.resumed")}</p>}
      {s.userChoice === "stop" && <p aria-live="polite">{t("ui.safestop.ended")}</p>}
    </section>
  );
}
