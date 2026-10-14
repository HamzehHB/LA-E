import { useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";

/**
 * §15 display-only Translate affordance (social-post style, blue).
 * No translation provider/backend capability exists in this project, so the
 * action never fabricates translated text and never mutates the original:
 * it truthfully reports that translation is unavailable and leaves the
 * persisted proposal / audit / staging content unchanged.
 */
export function TranslateAction() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [showNote, setShowNote] = useState(false);
  return (
    <div className="translate-row">
      <button
        type="button"
        className="translate-btn"
        aria-expanded={showNote}
        onClick={() => setShowNote((v) => !v)}
      >
        {t("ui.translate.action")}
      </button>
      {showNote && (
        <p role="note" className="translate-note">{t("ui.translate.unavailable")}</p>
      )}
    </div>
  );
}

export function ProcessingResult() {
  const { state, mockApprove, mockReject, mockEdit, mockSafeStop, mockFix, mockStopProcess, liveAvailable, liveApproval, liveApprove, liveEditProposal, liveError } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(state.run.proposal_body);
  const [draftTitle, setDraftTitle] = useState(state.run.proposal_title);
  const [acknowledged, setAcknowledged] = useState(false);
  const executable = state.run.proposal_action === "CREATE";
  const livePending = liveAvailable && liveApproval !== null;

  // §2/§3 genuine mode isolation. The synthetic fixture proposal and mock
  // actions appear ONLY in Test Mode with no live backend. In Vault Mode (or
  // once a backend is connected) the fixture is never shown as a real result;
  // an honest idle state appears until a real run produces a proposal.
  const showFixture = state.sourceMode === "test" && !liveAvailable;
  const mockAllowed = showFixture;
  const liveReview = state.run.review_state.startsWith("live");
  const showProposal = showFixture || livePending || liveReview;
  const safeStop = state.safeStop;

  const stateKind = safeStop || state.run.error ? "err"
    : livePending || showFixture ? "ok"
    : "idle";
  const stateLabel = stateKind === "err" ? t("ui.result.stateError")
    : stateKind === "ok" ? t("ui.result.stateReview")
    : t("ui.result.stateIdle");

  return (
    <section aria-label={t("ui.result.title")} className="panel result">
      <div className="panel-head">
        <h2>{t("ui.result.title")}</h2>
        <span className={`state-pill ${stateKind === "err" ? "err" : stateKind === "ok" ? "ok" : ""}`} aria-live="polite">
          {stateLabel}
        </span>
      </div>

      <RunControls />

      {safeStop ? (
        /* § Error / safe-stop: Fix & Stop live here, inside Processing Result.
         * Fix revalidates and resumes the SAME run; Stop ends it auditably. */
        <div className="safestop" role="alert">
          <p className="err-title">{t("ui.safestop.title")}</p>
          <p className="muted">run {safeStop.runId} · stage {safeStop.stage} · {safeStop.reason}</p>
          <div className="safestop-actions">
            <button type="button" className="fix" aria-label={t("ui.safestop.fix")} onClick={mockFix}>{t("ui.safestop.fix")}</button>
            <button type="button" className="stop" aria-label={t("ui.safestop.stop")} onClick={mockStopProcess}>{t("ui.safestop.stop")}</button>
          </div>
          {safeStop.userChoice === "fix" && safeStop.resumed && <p aria-live="polite">{t("ui.safestop.resumed")}</p>}
          {safeStop.userChoice === "stop" && <p aria-live="polite">{t("ui.safestop.ended")}</p>}
        </div>
      ) : showProposal ? (
        <>
          {showFixture && (
            <p role="note" className="demo-banner">
              {t("ui.demo.banner")}
            </p>
          )}
          <dl className="result-grid">
            <div className="result-field"><dt>{t("ui.result.class")}</dt><dd>{state.run.classification_type}</dd></div>
            <div className="result-field"><dt>{t("ui.result.proposalType")}</dt><dd>{state.run.proposal_action}</dd></div>
            <div className="result-field"><dt>{t("ui.result.confidence")}</dt><dd>{state.run.confidence_level}</dd></div>
          </dl>
          <h3>{t("ui.result.output")}</h3>
          <p className="output">{state.run.edited_body ?? state.run.proposal_body}</p>
          <TranslateAction />
          <p className="hash">proposal_hash: {state.run.proposal_hash} (display only; no authoritative re-hash in this foundation)</p>
          {executable ? (
            <div className="review" role="group" aria-label="human review">
              {livePending ? (
                <>
                  <p aria-live="polite" className="muted">live approval pending: {liveApproval!.proposal_hash}</p>
                  <button type="button" className="btn approve" onClick={() => void liveApprove("approve")}>{t("ui.review.approve")}</button>
                  <button type="button" className="btn reject" onClick={() => void liveApprove("reject")}>{t("ui.review.reject")}</button>
                  {liveError && <p role="alert">{liveError}</p>}
                  <button type="button" className="btn ghost" onClick={() => { setDraft(state.run.edited_body ?? state.run.proposal_body); setEditing((v) => !v); }}>
                    {t("ui.review.edit")}
                  </button>
                  {editing && (
                    <div>
                      <label htmlFor="edit-title">{t("ui.review.editTitle")}</label>
                      <input id="edit-title" value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} />
                      <label htmlFor="edit">{t("ui.review.editLiveBody")}</label>
                      <textarea id="edit" value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} />
                      <button type="button" className="btn" onClick={() => { void liveEditProposal(draftTitle, draft, ""); setEditing(false); }}>
                        {t("ui.review.saveEditLive")}
                      </button>
                    </div>
                  )}
                </>
              ) : mockAllowed ? (
                <>
                  <p className="muted"><small>{t("ui.review.mockNote")}</small></p>
                  <button type="button" className="btn approve" onClick={mockApprove}>{t("ui.review.approve")}</button>
                  <button type="button" className="btn reject" onClick={mockReject}>{t("ui.review.reject")}</button>
                  <button type="button" className="btn ghost" onClick={() => { setDraft(state.run.edited_body ?? state.run.proposal_body); setEditing((v) => !v); }}>
                    {t("ui.review.edit")}
                  </button>
                  <span aria-live="polite" className="muted">state: {state.run.review_state}</span>
                  {editing && (
                    <div>
                      <label htmlFor="edit-title">{t("ui.review.editTitle")}</label>
                      <input id="edit-title" value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} />
                      <label htmlFor="edit">{t("ui.review.editSample")}</label>
                      <textarea id="edit" value={draft} onChange={(e) => setDraft(e.target.value)} rows={4} />
                      <button type="button" className="btn" onClick={() => { mockEdit(draft); setEditing(false); }}>
                        {t("ui.review.saveEdit")}
                      </button>
                    </div>
                  )}
                  {/* Test-only affordance to exercise the error/safe-stop path. */}
                  <button type="button" className="btn ghost test-affordance" onClick={() => mockSafeStop(state.run.current_stage, "test dependency unavailable", "embedding-model")}>
                    {t("ui.demo.safeStop")}
                  </button>
                </>
              ) : (
                <span aria-live="polite" className="muted">state: {state.run.review_state}</span>
              )}
            </div>
          ) : (
            <div className="review" role="group" aria-label="human review">
              <p role="note" className="muted">{t("ui.review.nonExecutable")}</p>
              {acknowledged ? (
                <p aria-live="polite" className="muted">{t("ui.review.acknowledged")}</p>
              ) : (
                <button type="button" className="btn ghost" onClick={() => setAcknowledged(true)}>{t("ui.review.acknowledge")}</button>
              )}
            </div>
          )}
        </>
      ) : (
        <p role="note" className="muted">{t("ui.result.vaultIdle")}</p>
      )}
    </section>
  );
}

export function Journals() {
  const { state, liveAvailable, stepJournal, refreshJournals } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);

  // §2/§4 isolation: synthetic fixtures are shown ONLY in Test Mode with no
  // live backend. In Vault Mode with no backend, an honest empty state is
  // shown — fixtures are never presented as real Vault/Audit/Staging records.
  const showFixtures = !liveAvailable && state.sourceMode === "test";
  const vaultNoLive = state.sourceMode === "vault" && !liveAvailable;
  const audits = liveAvailable ? state.recentAudits : (showFixtures ? state.recentAudits : []);
  const staging = liveAvailable ? state.recentStaging : (showFixtures ? state.recentStaging : []);

  // Up to 10 eligible records; a 3-row visible window rotates every 5s.
  const window3 = <T,>(arr: T[], start: number): T[] => {
    if (arr.length === 0) return [];
    const n = Math.min(3, arr.length);
    const s = ((start % arr.length) + arr.length) % arr.length;
    return Array.from({ length: n }, (_, i) => arr[(s + i) % arr.length]);
  };
  const auditWindow = window3(audits, state.journalIndex);
  const stagingWindow = window3(staging, state.journalIndex);

  // §4 correlation: an audit's staging outcome is shown only when a staging
  // record shares the same stable id; otherwise it is "Unavailable" — never
  // inferred from timestamps or filenames.
  const correlate = (row: { id: string }): string => {
    const match = staging.find((s) => s.id === row.id);
    return match ? t(match.statusKey as Parameters<typeof translate>[1]) : t("ui.journals.unavailable");
  };

  const viewAllTitle = liveAvailable
    ? t("ui.journals.viewAllUnsupported")
    : t("ui.journals.viewAllOffline");

  return (
    <div className="journals">
      <section aria-label={t("ui.journals.audit")} className="panel journal">
        <div className="panel-head"><h2>{t("ui.journals.audit")}</h2></div>
        <table className="journal-table">
          <thead><tr><th>{t("ui.journals.time")}</th><th>{t("ui.journals.id")}</th><th>{t("ui.journals.result")}</th></tr></thead>
          <tbody>
            {vaultNoLive ? (
              <tr><td colSpan={3}>{t("ui.journals.vaultNoLive")}</td></tr>
            ) : auditWindow.length ? (
              auditWindow.map((r) => (
                <tr key={r.id}>
                  <td><time>{r.timestamp}</time></td>
                  <td>{r.id}{r.runId ? ` · ${r.runId}` : ""}</td>
                  <td>{t(r.statusKey as Parameters<typeof translate>[1])} · {correlate(r)}</td>
                </tr>
              ))
            ) : (
              <tr><td colSpan={3}>{t("ui.empty.journals")}</td></tr>
            )}
          </tbody>
        </table>
        <TranslateAction />
        <div className="journal-foot">
          <span className="pager">
            <button type="button" className="btn ghost pager-btn" aria-label={t("ui.journals.prev")} onClick={() => stepJournal(-1)}>‹</button>
            {audits.length ? state.journalIndex + 1 : 0} {t("ui.journals.pager")} {audits.length}
            <button type="button" className="btn ghost pager-btn" aria-label={t("ui.journals.next")} onClick={() => stepJournal(1)}>›</button>
          </span>
          <span className="pager">
            <button type="button" className="btn ghost pager-btn" onClick={() => void refreshJournals()}>{t("ui.journals.refresh")}</button>
            <button type="button" className="btn ghost" title={viewAllTitle} onClick={() => undefined}>{t("ui.journals.viewAll")}</button>
          </span>
        </div>
        <div className="journal-foot">
          <span title={t("ui.journals.example")}>⟳ 5s</span>
          {showFixtures && <span className="muted"><small>{t("ui.journals.example")}</small></span>}
        </div>
      </section>

      <section aria-label={t("ui.journals.staging")} className="panel journal">
        <div className="panel-head"><h2>{t("ui.journals.staging")}</h2></div>
        <table className="journal-table">
          <thead><tr><th>{t("ui.journals.time")}</th><th>{t("ui.journals.id")}</th><th>{t("ui.journals.status")}</th></tr></thead>
          <tbody>
            {vaultNoLive ? (
              <tr><td colSpan={3}>{t("ui.journals.vaultNoLive")}</td></tr>
            ) : stagingWindow.length ? (
              stagingWindow.map((r) => (
                <tr key={r.id}>
                  <td><time>{r.timestamp}</time></td>
                  <td>{r.id}{r.runId ? ` · ${r.runId}` : ""}</td>
                  <td>{t(r.statusKey as Parameters<typeof translate>[1])}</td>
                </tr>
              ))
            ) : (
              <tr><td colSpan={3}>{t("ui.empty.journals")}</td></tr>
            )}
          </tbody>
        </table>
        <TranslateAction />
        <div className="journal-foot">
          <span title={t("ui.journals.example")}>⟳ 5s</span>
          <button type="button" className="btn ghost" title={viewAllTitle} onClick={() => undefined}>{t("ui.journals.viewAll")}</button>
        </div>
        {showFixtures && <p className="muted"><small>{t("ui.journals.example")}</small></p>}
      </section>
    </div>
  );
}

export function Welcome() {
  const { state, setAppLanguage, setWelcomed } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (state.welcomed) return null;
  const sections: Array<[string, string]> = [
    ["Vault / Test", t("ui.welcome.vaultTest")],
    ["Staging", t("ui.welcome.staging")],
    ["Audit", t("ui.welcome.audit")],
    ["Database", t("ui.welcome.database")],
    ["AI", t("ui.welcome.ai")],
    ["Knowledge Language", t("ui.welcome.knowledgeLanguage")],
    ["Current Audit", t("ui.welcome.currentAudit")],
    ["Workflow", t("ui.welcome.workflow")],
    ["Processing Result", t("ui.welcome.processingResult")],
    ["Audit Journal", t("ui.welcome.auditJournal")],
    ["Staging Journal", t("ui.welcome.stagingJournal")],
    ["Composer", t("ui.welcome.composer")],
  ];
  return (
    <div className="welcome-overlay" role="dialog" aria-modal="true" aria-label={t("ui.welcome.title")}>
      <div className="panel welcome">
        <div className="brand-row">
          <span className="logo" aria-label="LA-E">
            <span className="logo-l" aria-hidden="true">L</span><span className="logo-a" aria-hidden="true">A</span><span className="logo-sep" aria-hidden="true">-</span><span className="logo-e" aria-hidden="true">E</span>
          </span>
          <span className="ver">v0.1</span>
        </div>
        <h2>{t("ui.welcome.title")}</h2>
        <p className="muted">{t("ui.welcome.intro")}</p>
        <div>
          <div className="muted">{t("ui.settings.appLanguage")}</div>
          <div role="radiogroup" aria-label={t("ui.settings.appLanguage")} className="review">
            <button type="button" role="radio" aria-checked={state.appLanguage === "en"} aria-label="English" onClick={() => setAppLanguage("en")}>English</button>
            <button type="button" role="radio" aria-checked={state.appLanguage === "fa"} aria-label="فارسی" onClick={() => setAppLanguage("fa")}>فارسی</button>
          </div>
        </div>
        <dl className="welcome-list">
          {sections.map(([name, desc]) => (
            <div key={name}><dt>{name}</dt><dd>{desc}</dd></div>
          ))}
        </dl>
        <button type="button" className="btn approve" onClick={setWelcomed}>{t("ui.welcome.enter")}</button>
      </div>
    </div>
  );
}

export function Composer() {
  const { state, setComposer, liveAvailable, startLiveRun, liveError } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (!state.composerOpen) {
    return (
      <div className="composer-collapsed">
        <button type="button" onClick={() => setComposer(true)}>✎ {t("ui.composer.placeholder")}</button>
      </div>
    );
  }
  // §3 focused mode: a bottom-sheet overlay that rises over the workflow /
  // journals. The dashboard stays mounted behind it, so run state, journals,
  // and scroll position are preserved; collapsing restores the layout. The
  // draft lives in the store (never lost). Processing begins only via the
  // real backend submit below — the animation itself starts nothing.
  return (
    <>
      <div className="composer-backdrop" aria-hidden="true" onClick={() => setComposer(false)} />
      <div className="composer-overlay" role="dialog" aria-label={t("ui.composer.placeholder")}>
        <div className="composer">
          <label htmlFor="composer">{t("ui.composer.placeholder")}</label>
          <textarea
            id="composer"
            rows={9}
            autoFocus
            value={state.composerText}
            onChange={(e) => setComposer(true, e.target.value)}
            placeholder={t("ui.composer.placeholderBox")}
          />
          <div className="composer-actions">
            {liveAvailable ? (
              <button type="button" className="btn approve" onClick={() => void startLiveRun(state.composerText)}>{t("ui.composer.submitLive")}</button>
            ) : (
              <>
                <button type="button" className="btn" disabled title={t("ui.composer.disabledHint")}>{t("ui.composer.submit")}</button>
                <span className="muted"><small>{t("ui.composer.disabledHint")}</small></span>
              </>
            )}
            <span className="spacer" />
            <button type="button" className="btn ghost" onClick={() => setComposer(false)}>{t("ui.composer.close")}</button>
          </div>
          {liveAvailable && liveError && <p role="alert">{liveError}</p>}
          <p className="responsibility">{t("ui.responsibility")}</p>
        </div>
      </div>
    </>
  );
}

export function SettingsPanel() {
  const { state, setTheme, setAppLanguage } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  return (
    <section aria-label={t("ui.settings.title")} className="panel settings settings-popover">
      <div className="panel-head"><h2>{t("ui.settings.title")}</h2></div>
      <div>
        <div className="muted">{t("ui.settings.display")}</div>
        <div role="radiogroup" aria-label={t("ui.settings.display")} className="review">
          <button type="button" role="radio" aria-checked={state.theme === "light"} aria-label={t("ui.settings.light")} onClick={() => setTheme("light")}>☀ {t("ui.settings.light")}</button>
          <button type="button" role="radio" aria-checked={state.theme === "dark"} aria-label={t("ui.settings.dark")} onClick={() => setTheme("dark")}>☾ {t("ui.settings.dark")}</button>
        </div>
      </div>
      <div>
        <div className="muted">{t("ui.settings.appLanguage")}</div>
        <div role="radiogroup" aria-label={t("ui.settings.appLanguage")} className="review">
          <button type="button" role="radio" aria-checked={state.appLanguage === "en"} aria-label="English" onClick={() => setAppLanguage("en")}>English</button>
          <button type="button" role="radio" aria-checked={state.appLanguage === "fa"} aria-label="فارسی" onClick={() => setAppLanguage("fa")}>فارسی</button>
        </div>
      </div>
    </section>
  );
}


export function OnboardingPanel() {
  const { state, mockOnboardingChoice, mockDismissOnboarding } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  // §2: simulated onboarding is a Test Mode affordance only; never shown as a
  // real action in Vault Mode.
  if (state.sourceMode === "vault") return null;
  if (state.onboarding.vectorBaselineDismissed || state.onboarding.vectorBaselineAccepted !== null) return null;
  return (
    <section aria-label="onboarding" className="panel onboard">
      <p>{t("ui.onboarding.vectorBaseline")}</p>
      <button type="button" onClick={() => mockOnboardingChoice("vectorBaselineAccepted", true)}>{t("ui.onboarding.accept")}</button>
      <button type="button" onClick={() => mockDismissOnboarding("vectorBaselineDismissed")}>{t("ui.onboarding.decline")}</button>
    </section>
  );
}

export function EnginePanel() {
  const { state, mockEngineChoice, mockDismissOnboarding } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  // §2: the LanceDB install prompt is a simulated Test Mode affordance only.
  if (state.sourceMode === "vault") return null;
  if (state.onboarding.engineDismissed || state.vectorEngine.status === "ready" || state.vectorEngine.status === "alternate_configured") return null;
  return (
    <section aria-label="engine" className="panel engine">
      <p>{t("ui.engine.installPrompt")}</p>
      <button type="button" onClick={() => mockEngineChoice("ready", "lancedb")}>{t("ui.engine.install")}</button>
      <button type="button" onClick={() => mockEngineChoice("alternate_configured", "other-engine")}>{t("ui.engine.chooseOther")}</button>
      <button type="button" onClick={() => { mockEngineChoice("user_declined"); mockDismissOnboarding("engineDismissed"); }}>{t("ui.onboarding.decline")}</button>
      {state.vectorEngine.status === "user_declined" && <p role="note">{t("ui.engine.declined")}</p>}
    </section>
  );
}

function vectorStatusLabel(s: string): { label: string; cls: string } {
  switch (s) {
    case "ready": return { label: "Ready", cls: "ok" };
    case "not_configured": return { label: "Not configured", cls: "" };
    case "engine_unavailable": return { label: "Engine unavailable", cls: "bad" };
    case "configured_empty": return { label: "Empty", cls: "warn" };
    case "failed": return { label: "Failed", cls: "bad" };
    case "stale_or_requires_rebuild": return { label: "Needs rebuild", cls: "warn" };
    default: return { label: s || "Unknown", cls: "" };
  }
}

export function ResourceStatusPanel() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const v = vectorStatusLabel(state.vectorIndex.status);
  return (
    <section aria-label="resources" className="panel">
      <div className="panel-head"><h2>{t("ui.vector.status")}</h2></div>
      <div className="review">
        <span className="badge">{t("ui.model.recommended")}: {t("ui.model.bge")}</span>
        <span className="badge">{t("ui.model.engineLance")}</span>
        <span className={`badge ${v.cls}`}>{v.label}</span>
        <span className={`badge ${state.llmAvailability.local === "ready" ? "ok" : state.llmAvailability.local === "not_installed" ? "bad" : "warn"}`}>{t("ui.llm.localStatus")}</span>
        <span className="badge">{t("ui.llm.cloudStatus")}</span>
      </div>
      <p className="muted">{t("ui.audit.layout")}</p>
      <h3>{t("ui.eligibility.title")}</h3>
      <p role="note" className="muted">{t("ui.eligibility.note")}</p>
      <p role="note" className="muted">{t("ui.sync.note")}</p>
    </section>
  );
}

export function RunControls() {
  const { state, liveAvailable, liveCancel } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [busy, setBusy] = useState(false);
  // Real, backend-backed cancellation is only offered for an actual live run:
  // a real run_id (never the synthetic fixture), not already stopped, and no
  // terminal event yet. This prevents cancelling a non-existent/fixture run.
  const terminal = state.runEvents.some(
    (e) => e.kind === "run_completed" || e.kind === "run_rejected");
  const realRun = !!state.run.run_id && !state.run.run_id.startsWith("run-fixture");
  const active = liveAvailable && realRun && !state.run.stopped && !terminal;
  if (!active) return null;
  const onCancel = async () => {
    if (busy) return; // prevent duplicate/inappropriate cancellation requests
    setBusy(true);
    try {
      await liveCancel();
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="run-controls" role="group" aria-label={t("ui.run.controls")}>
      <span className="muted">{state.run.status || t("ui.result.stateProcessing")}</span>
      <button type="button" className="btn reject" disabled={busy} onClick={() => void onCancel()}>
        {busy ? t("ui.run.stopping") : t("ui.run.stop")}
      </button>
    </div>
  );
}

export function SafeStopPanel() {
  const { state, mockSafeStop, mockFix, mockStopProcess } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  if (state.safeStop === null) {
    // §2: the "simulate safe-stop" trigger is a Test Mode affordance only; in
    // Vault Mode a real safe-stop (state.safeStop !== null) is still shown.
    if (state.sourceMode === "vault") return null;
    return (
      <div>
        <button type="button" onClick={() => mockSafeStop(state.run.current_stage, "test dependency unavailable", "embedding-model")}>
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

/**
 * Tiny corner capability status (§6). Purely informational — it never
 * authorizes anything and never implies a capability that is not actually
 * active. Shown very small in a page corner.
 */
export function StatusStrip() {
  const { state, liveAvailable } = useStore();
  const db = state.resources.vector_db.validity === "configured_valid";
  const embed = state.resources.embedding_model.validity === "configured_valid";
  const localLlm = state.resources.local_llm.validity === "configured_valid";
  const items: [string, boolean][] = [
    ["Backend", liveAvailable],
    ["DB", db],
    ["Embed", embed],
    ["Local LLM", localLlm],
  ];
  return (
    <div className="corner-status" role="status" aria-live="off">
      {items.map(([label, on]) => (
        <span key={label} className="cs-item" title={`${label}: ${on ? "active" : "not active"}`}>
          <span className={`cs-dot ${on ? "on" : ""}`} aria-hidden="true" />
          {label}
        </span>
      ))}
    </div>
  );
}
