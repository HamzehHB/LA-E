import { useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";

/**
 * Translate affordance calling live backend translation endpoint (/api/translate).
 */
export function TranslateAction({ text }: { text?: string }) {
  const { state, liveTranslate } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [loading, setLoading] = useState(false);
  const [translated, setTranslated] = useState("");
  const [open, setOpen] = useState(false);

  const handleTranslate = async () => {
    if (!text) {
      setOpen((v) => !v);
      return;
    }
    setLoading(true);
    const target = state.appLanguage === "fa" ? "fa" : "en";
    const res = await liveTranslate(text, target);
    setLoading(false);
    setTranslated(res);
    setOpen(true);
  };

  return (
    <div className="translate-row">
      <button
        type="button"
        className="translate-btn"
        aria-expanded={open}
        disabled={loading}
        onClick={() => void handleTranslate()}
      >
        {loading ? "Translating…" : t("ui.translate.action")}
      </button>
      {open && translated && (
        <div role="note" className="translate-result">
          <p>{translated}</p>
        </div>
      )}
    </div>
  );
}

export function ProcessingResult() {
  const {
    state,
    mockApprove,
    mockReject,
    mockEdit,
    mockSafeStop,
    mockFix,
    mockStopProcess,
    liveAvailable,
    liveApproval,
    liveApprove,
    liveEditProposal,
    liveAccept,
    liveFix,
    liveStop,
    liveError,
  } = useStore();

  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(state.run.proposal_body);
  const [draftTitle, setDraftTitle] = useState(state.run.proposal_title);
  const [showNoteModal, setShowNoteModal] = useState(false);
  const [fixInput, setFixInput] = useState("");

  const executable = state.run.proposal_action === "CREATE";
  const isReviewAction = state.run.proposal_action === "NEEDS_REVIEW" || state.run.proposal_action === "DO_NOT_IMPORT";
  const livePending = liveAvailable && liveApproval !== null;

  const showFixture = state.sourceMode === "test" && !liveAvailable;
  const mockAllowed = showFixture;
  const liveReview = state.run.review_state.startsWith("live");
  const showProposal = showFixture || livePending || liveReview;
  const safeStop = state.safeStop || (state.run.stopped && state.run.stop_reason);

  const stateKind = safeStop || state.run.error ? "err"
    : livePending || showFixture ? "ok"
    : "idle";
  const stateLabel = stateKind === "err" ? t("ui.result.stateError")
    : stateKind === "ok" ? t("ui.result.stateReview")
    : t("ui.result.stateIdle");

  const confScore = state.run.confidence_score !== undefined
    ? state.run.confidence_score.toFixed(2)
    : "0.89";
  const confBand = state.run.confidence_score_band || state.run.confidence_level || "high";

  return (
    <section aria-label={t("ui.result.title")} className="panel result-panel">
      <div className="panel-head">
        <div className="head-left">
          <h2>{t("ui.result.title")}</h2>
          <span className={`state-pill ${stateKind === "err" ? "err" : stateKind === "ok" ? "ok" : ""}`} aria-live="polite">
            {stateLabel}
          </span>
        </div>
        {showProposal && (
          <div className="badges-cluster">
            <span className="badge-chip class-chip">
              CLASS: <strong>{state.run.classification_type || "Observation"}</strong>
            </span>
            <span className="badge-chip action-chip">
              PROPOSAL TYPE: <strong>{state.run.proposal_action || "CREATE"}</strong>
            </span>
            <span className="badge-chip conf-chip">
              CONFIDENCE: <strong>{confScore}</strong> ({confBand})
            </span>
          </div>
        )}
      </div>

      {safeStop ? (
        /* Recoverable Safe-Stop / Paused Run recovery view */
        <div className="safestop-card" role="alert">
          <div className="safestop-header">
            <span className="error-icon" aria-hidden="true">⚠</span>
            <div>
              <p className="err-title">{t("ui.safestop.title")}</p>
              <p className="err-desc">
                stage: <strong>{state.run.current_stage || "pipeline"}</strong> · reason: {state.run.stop_reason || (state.safeStop ? state.safeStop.reason : "Execution halted")}
              </p>
            </div>
          </div>
          {state.safeStop && state.safeStop.userChoice !== "undecided" ? (
            <p role="note" className="muted safestop-feedback">
              {state.safeStop.userChoice === "fix"
                ? `Fix applied: dependency recovered. Note: the system cannot resume an interrupted run; submit a fresh input to process through the pipeline. (run: ${state.safeStop.runId})`
                : `Process stopped cleanly. No writes occurred. (run: ${state.safeStop.runId})`}
            </p>
          ) : (
            <>
              <div className="safestop-fix-box">
                <label htmlFor="fix-input">Correct input or parameters to resume:</label>
                <input
                  id="fix-input"
                  type="text"
                  className="input-field"
                  value={fixInput}
                  onChange={(e) => setFixInput(e.target.value)}
                  placeholder="Enter corrected query or instructions…"
                />
              </div>
              <div className="safestop-actions">
                {liveAvailable ? (
                  <>
                    <button type="button" className="btn approve" onClick={() => void liveFix(fixInput)}>
                      Fix & Resume
                    </button>
                    <button type="button" className="btn reject" onClick={() => void liveStop()}>
                      Stop Run
                    </button>
                  </>
                ) : (
                  <>
                    <button type="button" className="btn approve" onClick={mockFix}>
                      {t("ui.safestop.fix")}
                    </button>
                    <button type="button" className="btn reject" onClick={mockStopProcess}>
                      {t("ui.safestop.stop")}
                    </button>
                  </>
                )}
              </div>
            </>
          )}
        </div>
      ) : showProposal ? (
        <div className="proposal-content">
          {showFixture && (
            <p role="note" className="demo-banner">
              {t("ui.demo.banner")}
            </p>
          )}

          <div className="output-box">
            <p className="output-text">{state.run.edited_body ?? state.run.proposal_body}</p>
          </div>

          <div className="proposal-footer">
            <TranslateAction text={state.run.edited_body ?? state.run.proposal_body} />
            <p className="hash-label">
              proposal_hash: <code>{state.run.proposal_hash || "hash-pending"}</code>
            </p>
          </div>

          {/* Action Seams */}
          <div className="action-row" role="group" aria-label="human review">
            {executable ? (
              <>
                {livePending ? (
                  <>
                    <button type="button" className="btn approve" onClick={() => void liveApprove("approve")}>
                      {t("ui.review.approve")}
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => {
                        setDraft(state.run.edited_body ?? state.run.proposal_body);
                        setDraftTitle(state.run.proposal_title);
                        setEditing((v) => !v);
                      }}
                    >
                      {t("ui.review.edit")}
                    </button>
                    <button type="button" className="btn reject" onClick={() => void liveApprove("reject")}>
                      {t("ui.review.reject")}
                    </button>
                    <button type="button" className="btn ghost note-btn" onClick={() => setShowNoteModal(true)}>
                      Read Generated Note ↗
                    </button>
                    {liveError && <p role="alert" className="err-text">{liveError}</p>}
                  </>
                ) : mockAllowed ? (
                  <>
                    <button type="button" className="btn approve" onClick={mockApprove}>
                      {t("ui.review.approve")}
                    </button>
                    <button
                      type="button"
                      className="btn ghost"
                      onClick={() => {
                        setDraft(state.run.edited_body ?? state.run.proposal_body);
                        setDraftTitle(state.run.proposal_title);
                        setEditing((v) => !v);
                      }}
                    >
                      {t("ui.review.edit")}
                    </button>
                    <button type="button" className="btn reject" onClick={mockReject}>
                      {t("ui.review.reject")}
                    </button>
                    <button type="button" className="btn ghost note-btn" onClick={() => setShowNoteModal(true)}>
                      Read Generated Note ↗
                    </button>
                    <button
                      type="button"
                      className="btn ghost test-affordance"
                      onClick={() => mockSafeStop(state.run.current_stage, "test dependency unavailable", "embedding-model")}
                    >
                      {t("ui.demo.safeStop")}
                    </button>
                    <span aria-live="polite" className="muted review-state-badge">
                      mock state: {state.run.review_state}
                    </span>
                    {state.run.review_state === "mock_edited_stale" && (
                      <p role="note" className="muted">
                        Display only; no authoritative re-hash was performed in test mode.
                      </p>
                    )}
                  </>
                ) : (
                  <span className="muted">review state: {state.run.review_state}</span>
                )}
              </>
            ) : isReviewAction ? (
              <div className="review-acknowledge">
                <p className="review-hint">
                  This proposal is flagged as <strong>{state.run.proposal_action}</strong>. Human acknowledgment is required to complete the review.
                </p>
                {state.run.accepted ? (
                  <p aria-live="polite" className="ok-text">✓ {t("ui.review.acknowledged")}</p>
                ) : (
                  <button
                    type="button"
                    className="btn approve"
                    onClick={() => {
                      if (liveAvailable) void liveAccept();
                    }}
                  >
                    Accept
                  </button>
                )}
              </div>
            ) : (
              <div className="non-executable-row">
                <span className="muted">{t("ui.review.nonExecutable")}</span>
                {state.run.accepted ? (
                  <span className="ok-text">✓ {t("ui.review.acknowledged")}</span>
                ) : (
                  <button type="button" className="btn ghost" onClick={() => void liveAccept()}>
                    {t("ui.review.acknowledge")}
                  </button>
                )}
              </div>
            )}
          </div>

          {/* Inline Edit Form */}
          {editing && (
            <div className="edit-modal-box">
              <h4>Edit Proposal</h4>
              <label htmlFor="edit-title">{t("ui.review.editTitle")}</label>
              <input
                id="edit-title"
                className="input-field"
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
              />
              <label htmlFor="edit">{liveAvailable ? t("ui.review.editLiveBody") : t("ui.review.editSample")}</label>
              <textarea
                id="edit"
                className="textarea-field"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={5}
              />
              <div className="edit-actions">
                <button
                  type="button"
                  className="btn approve"
                  onClick={() => {
                    if (liveAvailable) {
                      void liveEditProposal(draftTitle, draft, "User edit via UI");
                    } else {
                      mockEdit(draft);
                    }
                    setEditing(false);
                  }}
                >
                  {liveAvailable ? t("ui.review.saveEditLive") : t("ui.review.saveEdit")}
                </button>
                <button type="button" className="btn ghost" onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="idle-state">
          <p className="muted">{t("ui.result.vaultIdle")}</p>
        </div>
      )}

      {/* Generated Note Reader Modal */}
      {showNoteModal && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="note-reader-title">
          <div className="modal-card reader-card">
            <div className="modal-header">
              <h3 id="note-reader-title">{state.run.proposal_title || "Generated Note"}</h3>
              <button type="button" className="close-btn" onClick={() => setShowNoteModal(false)}>✕</button>
            </div>
            <div className="modal-body note-markdown">
              <pre>{state.run.edited_body ?? state.run.proposal_body}</pre>
            </div>
            <div className="modal-footer">
              <button type="button" className="btn ghost" onClick={() => setShowNoteModal(false)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

export function Journals() {
  const { state, liveAvailable, stepJournal, refreshJournals } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);

  const [viewAllOpen, setViewAllOpen] = useState(false);

  const showFixtures = !liveAvailable && state.sourceMode === "test";
  const vaultNoLive = state.sourceMode === "vault" && !liveAvailable;
  const audits = liveAvailable ? state.recentAudits : (showFixtures ? state.recentAudits : []);
  const staging = liveAvailable ? state.recentStaging : (showFixtures ? state.recentStaging : []);

  const totalSlots = Math.max(audits.length, staging.length);
  const activeIdx = totalSlots > 0 ? (state.journalIndex % totalSlots) : 0;

  const currentAudit = audits[activeIdx];
  const currentStaging = staging[activeIdx];

  return (
    <div className="stacked-journals">
      {/* 1. Upper Journal: Summary Of Recent Audits */}
      <section aria-label={t("ui.journals.audit")} className="panel journal-panel audit-journal">
        <div className="panel-head">
          <div className="journal-title-row">
            <h2>Summary Of Recent Audits</h2>
            <span className="pager-indicator">
              [{totalSlots > 0 ? activeIdx + 1 : 0} of {totalSlots}]
            </span>
          </div>
          <div className="pager-nav">
            <button type="button" className="btn ghost small" aria-label={t("ui.journals.prev")} onClick={() => stepJournal(-1)}>‹</button>
            <button type="button" className="btn ghost small" aria-label={t("ui.journals.next")} onClick={() => stepJournal(1)}>›</button>
          </div>
        </div>

        <div className="journal-newspaper-card">
          {vaultNoLive ? (
            <p className="muted">{t("ui.journals.vaultNoLive")}</p>
          ) : currentAudit ? (
            <div className="newspaper-content">
              <div className="newspaper-meta">
                <span className="news-time">{currentAudit.timestamp ? currentAudit.timestamp.slice(0, 19).replace("T", " ") : "Recent"}</span>
                <span className="news-digest">digest: {currentAudit.digest || currentAudit.id.slice(0, 12)}</span>
                <span className={`status-badge ${currentAudit.statusKey === "journal.passed" ? "ok" : "failed"}`}>
                  {t(currentAudit.statusKey as Parameters<typeof translate>[1])}
                </span>
              </div>
              <h4 className="news-title">{currentAudit.title || `Audit Record: ${currentAudit.id}`}</h4>
              <p className="news-summary">{currentAudit.summary || `Audit record registered in category ${currentAudit.statusKey}.`}</p>
              <TranslateAction text={currentAudit.summary || currentAudit.title} />
            </div>
          ) : (
            <p className="muted">{t("ui.empty.journals")}</p>
          )}
        </div>

        <div className="journal-foot">
          <span className="sync-timer">⟳ 5s synchronized</span>
          <button
            type="button"
            className="btn ghost small"
            title={t(state.sourceMode === "test" ? "ui.journals.viewAllOffline" : "ui.journals.viewAllUnsupported")}
            onClick={() => setViewAllOpen(true)}
          >
            {t("ui.journals.viewAll")}
          </button>
        </div>
      </section>

      {/* 2. Lower Journal: Summary Of Recent Staging */}
      <section aria-label={t("ui.journals.staging")} className="panel journal-panel staging-journal">
        <div className="panel-head">
          <div className="journal-title-row">
            <h2>Summary Of Recent Staging</h2>
            <span className="pager-indicator">
              [{totalSlots > 0 ? activeIdx + 1 : 0} of {totalSlots}]
            </span>
          </div>
          <div className="pager-nav">
            <button type="button" className="btn ghost small" aria-label={t("ui.journals.prev")} onClick={() => stepJournal(-1)}>‹</button>
            <button type="button" className="btn ghost small" aria-label={t("ui.journals.next")} onClick={() => stepJournal(1)}>›</button>
          </div>
        </div>

        <div className="journal-newspaper-card">
          {vaultNoLive ? (
            <p className="muted">{t("ui.journals.vaultNoLive")}</p>
          ) : currentStaging ? (
            <div className="newspaper-content">
              <div className="newspaper-meta">
                <span className="news-time">{currentStaging.timestamp ? currentStaging.timestamp.slice(0, 19).replace("T", " ") : "Recent"}</span>
                <span className={`status-badge ${currentStaging.staging_available ? "ok" : "unavailable"}`}>
                  {currentStaging.staging_available ? "Staged" : "Unavailable"}
                </span>
              </div>
              <h4 className="news-title">{currentStaging.title || "Staged Artifact"}</h4>
              <p className="news-summary excerpt">
                {currentStaging.excerpt || (currentStaging.staging_available ? "Staging file generated and ready for human review." : "Unavailable — no staged note produced.")}
              </p>
              <TranslateAction text={currentStaging.excerpt || currentStaging.title} />
            </div>
          ) : (
            <p className="muted">{t("ui.empty.journals")}</p>
          )}
        </div>

        <div className="journal-foot">
          <button type="button" className="btn ghost small" onClick={() => void refreshJournals()}>
            {t("ui.journals.refresh")}
          </button>
          <button
            type="button"
            className="btn ghost small"
            title={t(state.sourceMode === "test" ? "ui.journals.viewAllOffline" : "ui.journals.viewAllUnsupported")}
            onClick={() => setViewAllOpen(true)}
          >
            {t("ui.journals.viewAll")}
          </button>
        </div>
      </section>

      {/* View All Journals Modal */}
      {viewAllOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="viewall-title">
          <div className="modal-card wide-card">
            <div className="modal-header">
              <h3 id="viewall-title">All Historical Audits & Staging Records</h3>
              <button type="button" className="close-btn" onClick={() => setViewAllOpen(false)}>✕</button>
            </div>
            <div className="modal-body">
              <h4>Audits Trail (Passed & Failed)</h4>
              <table className="journal-table">
                <thead>
                  <tr>
                    <th>Timestamp</th>
                    <th>ID / Digest</th>
                    <th>Outcome</th>
                    <th>Reference</th>
                  </tr>
                </thead>
                <tbody>
                  {audits.map((a) => (
                    <tr key={a.id}>
                      <td><time>{a.timestamp}</time></td>
                      <td>{a.digest || a.id}</td>
                      <td>{t(a.statusKey as Parameters<typeof translate>[1])}</td>
                      <td>{a.artifactReference || "—"}</td>
                    </tr>
                  ))}
                  {audits.length === 0 && (
                    <tr><td colSpan={4}>No audit records logged yet.</td></tr>
                  )}
                </tbody>
              </table>

              <h4 style={{ marginTop: "1.5rem" }}>Staging Artifacts</h4>
              <table className="journal-table">
                <thead>
                  <tr>
                    <th>Timestamp</th>
                    <th>Title / Reference</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {staging.map((s) => (
                    <tr key={s.id}>
                      <td><time>{s.timestamp}</time></td>
                      <td>{s.title || s.artifactReference || s.id}</td>
                      <td>{s.staging_available ? "Staged" : "Unavailable"}</td>
                    </tr>
                  ))}
                  {staging.length === 0 && (
                    <tr><td colSpan={3}>No staging records available yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
            <div className="modal-footer">
              <button type="button" className="btn ghost" onClick={() => setViewAllOpen(false)}>Close</button>
            </div>
          </div>
        </div>
      )}
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
      <div className="panel welcome-card">
        <div className="brand-row">
          <span className="logo" aria-label="LA-E">
            <span className="logo-l" aria-hidden="true">L</span>
            <span className="logo-a" aria-hidden="true">A</span>
            <span className="logo-sep" aria-hidden="true">-</span>
            <span className="logo-e" aria-hidden="true">E</span>
          </span>
          <span className="ver">v0.1</span>
        </div>
        <h2>{t("ui.welcome.title")}</h2>
        <p className="welcome-intro">{t("ui.welcome.intro")}</p>

        <div className="welcome-lang-box">
          <span className="note">{t("ui.settings.appLanguage")}:</span>
          <div role="radiogroup" aria-label={t("ui.settings.appLanguage")} className="button-group">
            <button
              type="button"
              role="radio"
              className={state.appLanguage === "en" ? "selected" : ""}
              aria-checked={state.appLanguage === "en"}
              aria-label="English"
              onClick={() => setAppLanguage("en")}
            >
              English
            </button>
            <button
              type="button"
              role="radio"
              className={state.appLanguage === "fa" ? "selected" : ""}
              aria-checked={state.appLanguage === "fa"}
              aria-label="فارسی"
              onClick={() => setAppLanguage("fa")}
            >
              فارسی
            </button>
          </div>
        </div>

        <dl className="welcome-list">
          {sections.map(([name, desc]) => (
            <div key={name} className="welcome-item">
              <dt>{name}</dt>
              <dd>{desc}</dd>
            </div>
          ))}
        </dl>
        <button type="button" className="btn approve enter-btn" onClick={setWelcomed}>
          {t("ui.welcome.enter")}
        </button>
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
        <button type="button" className="composer-trigger" onClick={() => setComposer(true)}>
          ✎ {t("ui.composer.placeholder")}
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="composer-backdrop" aria-hidden="true" onClick={() => setComposer(false)} />
      <div className="composer-overlay" role="dialog" aria-label={t("ui.composer.placeholder")}>
        <div className="composer-sheet">
          <div className="composer-header">
            <label htmlFor="composer-textarea" className="composer-label">
              {t("ui.composer.placeholder")}
            </label>
            <button type="button" className="close-btn" onClick={() => setComposer(false)}>✕</button>
          </div>
          <textarea
            id="composer-textarea"
            rows={7}
            autoFocus
            className="composer-textarea"
            value={state.composerText}
            onChange={(e) => setComposer(true, e.target.value)}
            placeholder={t("ui.composer.placeholderBox")}
          />
          <div className="composer-actions">
            {liveAvailable ? (
              <button
                type="button"
                className="btn approve submit-btn"
                onClick={() => {
                  void startLiveRun(state.composerText);
                  setComposer(false);
                }}
              >
                {t("ui.composer.submitLive")}
              </button>
            ) : (
              <>
                <button type="button" className="btn submit-btn" disabled title={t("ui.composer.disabledHint")}>
                  {t("ui.composer.submit")}
                </button>
                <span className="muted"><small>{t("ui.composer.disabledHint")}</small></span>
              </>
            )}
            <span className="spacer" />
            <button type="button" className="btn ghost" onClick={() => setComposer(false)}>
              {t("ui.composer.close")}
            </button>
          </div>
          {liveAvailable && liveError && <p role="alert" className="err-text">{liveError}</p>}
          <p className="responsibility">{t("ui.responsibility")}</p>
        </div>
      </div>
    </>
  );
}
