import { useState, useEffect, useRef, useMemo, startTransition } from "react";
import { useStore } from "../state/store";
import { translate, hasUiKey, ERROR_CODE_TO_KEY, type UiKey } from "../i18n/resources";

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
    dismissRunOutcome,
    cancelActiveRun,
  } = useStore();

  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(state.run.proposal_body);
  const [draftTitle, setDraftTitle] = useState(state.run.proposal_title);
  const [showNoteModal, setShowNoteModal] = useState(false);
  const [fixQueryInput, setFixQueryInput] = useState("");

  const executable = state.run.proposal_action === "CREATE";
  const isReviewAction = state.run.proposal_action === "NEEDS_REVIEW" || state.run.proposal_action === "DO_NOT_IMPORT";
  const livePending = liveAvailable && liveApproval !== null;

  const isFixtureRun = !state.run.run_id || state.run.run_id.startsWith("run-fixture");
  const showFixture = state.sourceMode === "test" && !liveAvailable && isFixtureRun;
  const mockProposalActive = Boolean(state.run.proposal_body && (state.run.review_state.startsWith("mock") || state.run.review_state === "awaiting_review") && !isFixtureRun);
  const mockAllowed = showFixture || mockProposalActive;
  const liveReview = state.run.review_state.startsWith("live");
  const showProposal = showFixture || livePending || liveReview || mockProposalActive;

  const hasInputOutcome = Boolean(state.inputOutcome);
  const isClarification = state.inputOutcome === "NEEDS_CLARIFICATION";
  const safeStop = !hasInputOutcome && (state.safeStop || (state.run.stopped && state.run.stop_reason));

  const isRunning =
    liveAvailable &&
    !isFixtureRun &&
    !state.run.stopped &&
    !safeStop &&
    !hasInputOutcome &&
    !state.run.error &&
    state.run.current_stage !== "completed" &&
    !state.run.status.toLowerCase().includes("finished") &&
    !state.run.status.toLowerCase().includes("terminal") &&
    !state.run.status.toLowerCase().includes("completed") &&
    !state.run.status.toLowerCase().includes("already completed") &&
    !livePending &&
    !showFixture &&
    !liveReview;

  const stateKind =
    safeStop || state.run.error || (hasInputOutcome && !isClarification)
      ? "err"
      : isClarification
      ? "processing"
      : livePending || showFixture
      ? "ok"
      : isRunning
      ? "processing"
      : "idle";

  const stateLabel =
    state.inputOutcome === "STOPPED" || (state.run.stopped && !hasInputOutcome && safeStop)
      ? t("ui.stage.stopped")
      : isClarification
      ? t("ui.recovery.clarificationTitle")
      : stateKind === "err"
      ? t("ui.result.stateError")
      : stateKind === "ok"
      ? t("ui.result.stateReview")
      : stateKind === "processing"
      ? t("ui.result.stateProcessing")
      : t("ui.result.stateIdle");

  const recoveryTitle =
    state.inputOutcome === "REJECTED"
      ? t("ui.recovery.rejectedTitle")
      : state.inputOutcome === "NEEDS_CLARIFICATION"
      ? t("ui.recovery.clarificationTitle")
      : state.inputOutcome === "STOPPED"
      ? t("ui.recovery.stoppedTitle")
      : t("ui.safestop.title");

  const badgeLabel =
    state.inputOutcome === "REJECTED"
      ? "REJECTED"
      : state.inputOutcome === "NEEDS_CLARIFICATION"
      ? "NEEDS CLARIFICATION"
      : state.inputOutcome === "STOPPED"
      ? "STOPPED"
      : "ERROR";

  const localizedErrorMessage = useMemo(() => {
    if (state.inputErrorCode) {
      const mapped = ERROR_CODE_TO_KEY[state.inputErrorCode];
      if (mapped) return t(mapped);
      const rawKey = `input.error.${state.inputErrorCode}` as UiKey;
      if (hasUiKey(rawKey)) return t(rawKey);
    }
    return state.run.stop_reason || state.run.status || t("ui.safestop.title");
  }, [state.inputErrorCode, state.run.stop_reason, state.run.status, state.appLanguage]);

  const confScore = state.run.confidence_score !== undefined
    ? state.run.confidence_score.toFixed(2)
    : "0.89";
  const confBand = state.run.confidence_score_band || state.run.confidence_level || "high";

  return (
    <section aria-label={t("ui.result.title")} className="panel result-panel">
      <div className="panel-head">
        <div className="head-left">
          <h2>{t("ui.result.title")}</h2>
          <span className={`state-pill ${stateKind === "err" ? "err" : stateKind === "ok" ? "ok" : stateKind === "processing" ? "processing" : ""}`} aria-live="polite">
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

      <div className="result-panel-body">
        {hasInputOutcome ? (
          /* Actionable Recovery Panel for backend-authoritative evaluation outcomes */
          <div className="safestop-card recovery-card" role="alert" aria-live="polite">
            <div className="safestop-header">
              <span className="error-icon" aria-hidden="true">
                {state.inputOutcome === "NEEDS_CLARIFICATION" ? "ℹ" : "⚠"}
              </span>
              <div>
                <p className="err-title">{recoveryTitle}</p>
                <div className="recovery-badges" style={{ display: "flex", gap: "0.5rem", alignItems: "center", margin: "0.25rem 0", flexWrap: "wrap" }}>
                  <span className={`badge-chip outcome-chip ${state.inputOutcome ? state.inputOutcome.toLowerCase() : ""}`}>
                    [{badgeLabel}]
                  </span>
                  {state.inputErrorCode && (
                    <span className="badge-chip code-chip">
                      <code>{state.inputErrorCode}</code>
                    </span>
                  )}
                  {state.run.run_id && (
                    <span className="muted" style={{ fontSize: "0.75rem" }}>
                      run: <code>{state.run.run_id}</code>
                    </span>
                  )}
                </div>
                <p className="err-desc">{localizedErrorMessage}</p>
              </div>
            </div>

            {Boolean(state.lastSubmittedText) && (
              <div className="recovery-draft-box" style={{ margin: "0.75rem 0", padding: "0.5rem 0.75rem", background: "var(--card-bg, rgba(255,255,255,0.05))", borderRadius: "4px", border: "1px solid var(--border-color, #444)" }}>
                <span className="muted draft-label" style={{ fontSize: "0.75rem", display: "block", marginBottom: "0.25rem" }}>
                  {t("ui.recovery.previousDraft")}:
                </span>
                <blockquote className="recovery-draft-quote" style={{ margin: 0, fontStyle: "italic", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                  {state.lastSubmittedText}
                </blockquote>
              </div>
            )}

            <div className="safestop-actions recovery-actions">
              <button
                type="button"
                className="btn ghost dismiss-btn"
                onClick={dismissRunOutcome}
              >
                {t("ui.recovery.dismiss")}
              </button>
            </div>
          </div>
      ) : safeStop ? (
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
                  value={fixQueryInput}
                  onChange={(e) => setFixQueryInput(e.target.value)}
                  placeholder="Enter corrected query or instructions…"
                />
              </div>
              <div className="safestop-actions">
                {liveAvailable ? (
                  <>
                    <button type="button" className="btn approve" onClick={() => void liveFix(fixQueryInput)}>
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
      ) : isRunning ? (
        <div className="processing-card" role="status" aria-live="polite">
          <div className="processing-indicator">
            <span className="processing-spinner" aria-hidden="true" />
          </div>
          <div className="processing-info">
            <p className="processing-stage-label">
              {t("ui.result.processingStage")}: <strong className="stage-highlight">{state.run.current_stage ? state.run.current_stage.toUpperCase() : "INPUT"}</strong>
            </p>
            <p className="processing-status-text">
              {state.run.status || t("ui.result.processingDesc")}
            </p>
            <p className="muted processing-meta">
              run: <code>{state.run.run_id}</code>
            </p>
            <div className="processing-actions" style={{ marginTop: "0.5rem" }}>
              <button
                type="button"
                className="btn reject small"
                onClick={() => void cancelActiveRun()}
              >
                {t("ui.recovery.stop")}
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="idle-state">
          <p className="muted">
            {state.sourceMode === "vault"
              ? t("ui.result.vaultIdle")
              : t("ui.result.testIdle")}
          </p>
        </div>
      )}
      </div>

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

  interface GuideItem {
    id: string;
    titleKey: Parameters<typeof translate>[1];
    descKey: Parameters<typeof translate>[1];
  }

  interface GuideGroup {
    id: string;
    titleKey: Parameters<typeof translate>[1];
    items: GuideItem[];
  }

  const groups: GuideGroup[] = [
    {
      id: "modes",
      titleKey: "ui.welcome.group.modes",
      items: [
        { id: "test", titleKey: "ui.welcome.modeTest.title", descKey: "ui.welcome.modeTest.desc" },
        { id: "vault", titleKey: "ui.welcome.modeVault.title", descKey: "ui.welcome.modeVault.desc" },
      ],
    },
    {
      id: "config",
      titleKey: "ui.welcome.group.config",
      items: [
        { id: "staging", titleKey: "ui.welcome.staging.title", descKey: "ui.welcome.staging.desc" },
        { id: "audit", titleKey: "ui.welcome.audit.title", descKey: "ui.welcome.audit.desc" },
        { id: "database", titleKey: "ui.welcome.database.title", descKey: "ui.welcome.database.desc" },
        { id: "ai", titleKey: "ui.welcome.ai.title", descKey: "ui.welcome.ai.desc" },
        { id: "knowledgeLanguage", titleKey: "ui.welcome.knowledgeLanguage.title", descKey: "ui.welcome.knowledgeLanguage.desc" },
      ],
    },
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

        <div className="welcome-sections" tabIndex={0} role="region" aria-label={t("ui.welcome.title")}>
          {groups.map((grp) => (
            <section key={grp.id} className="welcome-group" aria-labelledby={`welcome-grp-${grp.id}`}>
              <h3 id={`welcome-grp-${grp.id}`} className="welcome-group-title">
                {t(grp.titleKey)}
              </h3>
              <dl className="welcome-group-grid">
                {grp.items.map((item) => (
                  <div key={item.id} className="welcome-item">
                    <dt>{t(item.titleKey)}</dt>
                    <dd>{t(item.descKey)}</dd>
                  </div>
                ))}
              </dl>
            </section>
          ))}
        </div>

        <button type="button" className="btn approve enter-btn" onClick={setWelcomed}>
          {t("ui.welcome.enter")}
        </button>
      </div>
    </div>
  );
}

export function Composer() {
  const {
    state,
    setComposer,
    liveAvailable,
    startLiveRun,
    liveError,
  } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);

  const [text, setText] = useState(state.composerText);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Sync external changes (e.g. if store clears text on submit or external reset)
  useEffect(() => {
    setText(state.composerText);
    if (!state.composerText && textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  }, [state.composerText]);

  // Pure client-side UTF-8 byte calculation: 0ms, zero lag, no backend needed
  const byteCount = useMemo(() => {
    return new TextEncoder().encode(text).length;
  }, [text]);

  const MIN_LETTERS = 50;
  const { letterCount, tokenCount } = useMemo(() => {
    const letters = text.match(/\p{L}/gu);
    const tokens = text.match(/[\p{L}\p{N}_\u200c]+/gu);
    return {
      letterCount: letters ? letters.length : 0,
      tokenCount: tokens ? tokens.length : 0,
    };
  }, [text]);
  const hasMinLetters = letterCount >= MIN_LETTERS;

  const isOversized = byteCount > 1_000_000;
  const isFixtureRun = !state.run.run_id || state.run.run_id.startsWith("run-fixture");

  const runActive =
    liveAvailable &&
    !isFixtureRun &&
    !state.run.stopped &&
    Boolean(state.run.status) &&
    !state.run.status.toLowerCase().includes("finished") &&
    !state.run.status.toLowerCase().includes("terminal") &&
    !state.run.status.toLowerCase().includes("completed") &&
    !state.run.status.toLowerCase().includes("already completed") &&
    state.run.current_stage !== "completed";

  const canSubmit = !runActive && hasMinLetters && !isOversized;

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    // Immediate local state update: absolutely zero input lag
    setText(val);

    // Auto-grow textarea smoothly without jumping
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height = `${Math.min(textareaRef.current.scrollHeight, 120)}px`;
    }

    // Non-blocking sync to global store so background listeners remain updated
    startTransition(() => {
      setComposer(true, val);
    });
  };

  const handleSubmit = (e?: React.FormEvent) => {
    if (e) e.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || !hasMinLetters || isOversized || runActive) return;

    if (liveAvailable) {
      setText("");
      if (textareaRef.current) {
        textareaRef.current.style.height = "auto";
      }
      setComposer(true, "");
      void startLiveRun(trimmed);
    } else {
      // Backend is unavailable: retain draft in composer, do not clear
      void startLiveRun(trimmed);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (canSubmit) {
        handleSubmit();
      }
    }
  };

  return (
    <div className="composer-inplace">
      <form
        className="composer-form"
        onSubmit={handleSubmit}
        role="region"
        aria-label={t("ui.welcome.composer.title")}
      >
        <div className="composer-input-row">
          <textarea
            ref={textareaRef}
            id="chat-composer-textarea"
            className="composer-inplace-textarea"
            rows={1}
            value={text}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            placeholder={t("ui.composer.placeholder")}
            disabled={runActive}
            aria-label={t("ui.composer.placeholder")}
            autoComplete="off"
            spellCheck="false"
          />
          <div className="composer-actions">
            <button
              type="submit"
              className="btn approve submit-btn"
              disabled={!canSubmit}
              title={
                runActive
                  ? t("ui.composer.runningHint")
                  : !hasMinLetters && text.length > 0
                  ? state.appLanguage === "fa"
                    ? `حداقل ۵۰ حرف برای ارسال الزامی است (فعلی: ${letterCount} حرف)`
                    : `Minimum 50 letters required to send (current: ${letterCount} letters)`
                  : liveAvailable
                  ? t("ui.composer.submitLive")
                  : t("ui.composer.submitTestHint")
              }
            >
              {runActive
                ? t("ui.composer.running")
                : liveAvailable
                ? t("ui.composer.submitLive")
                : t("ui.composer.submit")}
            </button>
          </div>
        </div>
        {text.length > 0 && (
          <div className="composer-meta-row" aria-live="polite">
            {isOversized ? (
              <span className="composer-oversized-badge" role="alert">
                ⚠️ {t("ui.composer.oversized")} ({(byteCount / 1024).toFixed(1)} KB)
              </span>
            ) : (
              <>
                <span className={`composer-word-badge ${hasMinLetters ? "word-count-ok" : "word-count-needed"}`}>
                  {state.appLanguage === "fa"
                    ? `${letterCount} / ۵۰ حرف (${tokenCount} توکن)${hasMinLetters ? " — آماده ارسال" : " — نیاز به داده بیشتر"}`
                    : `${letterCount} / 50 letters (${tokenCount} tokens)${hasMinLetters ? " — ready to send" : " — more data needed"}`}
                </span>
                <span className="composer-char-badge">
                  {text.length} {state.appLanguage === "fa" ? "نویسه" : "chars"} ({byteCount} B)
                </span>
              </>
            )}
          </div>
        )}
        {liveError && (
          <p role="alert" className="err-text composer-err">
            {liveError === "connection_unavailable" ? t("input.error.connection_unavailable") : liveError}
          </p>
        )}
      </form>
      <p className="responsibility">{t("ui.responsibility")}</p>
    </div>
  );
}
