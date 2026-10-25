import { useEffect, useRef, useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";
import { deriveWorkflow, isTerminalSuccess, deriveActiveRetrievalSubstep, deriveRetrievalSubsteps } from "./workflowStages";

function eventLine(kind: string, stage: string, message: string, operation?: string): string {
  const where = stage ? (operation ? ` [${stage}:${operation}]` : ` [${stage}]`) : "";
  return `${where} ${message || kind}`.trim();
}

export function CurrentAudit() {
  const { state, liveCancel, liveAvailable } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [collapsed, setCollapsed] = useState(false);
  const [enlarged, setEnlarged] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const events = state.runEvents;
  const activeSubstep = deriveActiveRetrievalSubstep(events, state.run.run_id, Boolean(state.run.stopped));

  const runActive =
    liveAvailable &&
    Boolean(state.run.run_id) &&
    !state.run.stopped &&
    Boolean(state.run.status) &&
    !state.run.status.toLowerCase().includes("finished") &&
    !state.run.status.toLowerCase().includes("terminal") &&
    !state.run.status.toLowerCase().includes("completed") &&
    !state.run.status.toLowerCase().includes("already completed") &&
    state.run.current_stage !== "completed";

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length, state.run.status, state.run.stop_reason]);

  // §9/§13: Current Audit exists ONLY while a run is producing events.
  // When idle (no events), it is hidden entirely and the 13 stars show.
  if (events.length === 0) return null;

  return (
    <section
      aria-label={t("ui.current.title")}
      className={`panel current-audit-overlay${enlarged ? " enlarged" : ""}${collapsed ? " collapsed" : ""}`}
    >
      <div className="panel-head">
        <div className="title-cluster">
          <span className="pulsing-radar" aria-hidden="true" />
          <h2>{t("ui.current.title")}</h2>
          <span className="badge ok">{state.run.current_stage || "active"}</span>
          {state.run.current_stage === "retrieval" && activeSubstep && (
            <span
              className={`badge info ${activeSubstep.isRunning ? "running" : activeSubstep.phase === "completed" ? "completed" : "unknown"}`}
              data-phase={activeSubstep.phase}
            >
              {activeSubstep.isRunning ? "▶ " : activeSubstep.phase === "completed" ? "✓ " : "• "}
              {activeSubstep.operation.replace(/_/g, " ")} {activeSubstep.isRunning ? "(running)" : activeSubstep.phase === "completed" ? "(completed)" : `(${activeSubstep.phase})`}
            </span>
          )}
        </div>
        <div className="head-controls">
          {runActive && (
            <button
              type="button"
              className="btn small danger cancel-run-btn"
              title={t("ui.run.cancelTitle")}
              onClick={() => void liveCancel()}
            >
              ⏹ {t("ui.run.cancel")}
            </button>
          )}
          <button
            type="button"
            className="btn ghost small"
            aria-expanded={enlarged}
            aria-label={t("ui.current.expandLarge")}
            onClick={() => setEnlarged((v) => !v)}
          >
            {enlarged ? "▲" : "▼"}
          </button>
          <button
            type="button"
            className="btn ghost small"
            aria-expanded={!collapsed}
            aria-label={t("ui.current.collapse")}
            onClick={() => setCollapsed((v) => !v)}
          >
            {collapsed ? "▲" : "▼"}
          </button>
        </div>
      </div>

      {!collapsed && (
        <div className="audit-log" aria-live="polite" ref={logRef}>
          {events.length === 0 && (
            <div className="log-line empty">
              [{state.run.run_id}] stage={state.run.current_stage || "—"} status={state.run.status || "—"}
            </div>
          )}
          {events.map((e, i) => (
            <div key={i} className={`ev ev-${e.kind}`}>
              <span className="ev-time">{e.timestamp ? e.timestamp.slice(11, 19) : ""}</span>
              <span className="ev-body">{eventLine(e.kind, e.stage, e.message, e.operation)}</span>
            </div>
          ))}
          {state.run.stop_reason && (
            <div className="ev ev-stopped">
              <span className="ev-body">[stopped] {state.run.stop_reason}</span>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

export function Workflow() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const stages = deriveWorkflow(state.run, state.runEvents);
  const success = isTerminalSuccess(state.runEvents);
  const isRejected = state.run.stopped || state.run.review_state === "live_rejected" || state.run.review_state === "mock_rejected";
  const retrievalStage = stages.find((s) => s.key === "retrieval");
  const retrievalSubsteps = deriveRetrievalSubsteps(
    retrievalStage?.state ?? "pending",
    state.runEvents,
    state.run.run_id,
  );

  return (
    <section aria-label={t("ui.workflow.title")} className="panel workflow-panel">
      <div className="panel-head">
        <h2>{t("ui.workflow.title")}</h2>
      </div>
      <div className="workflow-scroll">
        <ol
          className="timeline"
          data-terminal={success ? "success" : isRejected ? "rejected" : undefined}
        >
          {stages.map((s) => {
            const showRetrievalSubsteps =
              s.key === "retrieval" &&
              (s.state === "current" ||
               s.state === "stopped" ||
               retrievalSubsteps.some((sub) => sub.state !== "pending"));
            return (
              <li
                key={s.key}
                data-state={s.state}
                className={showRetrievalSubsteps ? "has-substeps" : undefined}
                aria-label={`${s.label}: ${t(
                  (s.state === "completed"
                    ? "ui.stage.completed"
                    : s.state === "current"
                      ? "ui.stage.current"
                      : s.state === "stopped"
                        ? "ui.stage.stopped"
                        : "ui.stage.pending") as Parameters<typeof translate>[1]
                )}`}
              >
                <span className="node" aria-hidden="true" />
                <div className="timeline-content">
                  <span className="grp">{s.label}</span>
                  {showRetrievalSubsteps && (
                    <ol className="timeline-substeps" aria-label="Retrieval Substeps">
                      {retrievalSubsteps.map((sub) => (
                        <li
                          key={sub.key}
                          className={`substep-pill substep-${sub.state}`}
                          data-substep={sub.key}
                          data-state={sub.state}
                        >
                          <span className="substep-dot" aria-hidden="true" />
                          <span className="substep-label">{sub.label}</span>
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      </div>
    </section>
  );
}
