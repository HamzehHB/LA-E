import { useEffect, useRef, useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";
import { deriveWorkflow, isTerminalSuccess } from "./workflowStages";

function eventLine(kind: string, stage: string, message: string): string {
  const where = stage ? ` ${stage}` : "";
  return `[${kind}]${where} ${message}`.trim();
}

export function CurrentAudit() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [collapsed, setCollapsed] = useState(false);
  const [enlarged, setEnlarged] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  const events = state.runEvents;
  // §9/§13: Current Audit exists ONLY while a real run is producing events.
  // When idle (no events), it is hidden entirely and the 13 stars show.
  const idle = events.length === 0;

  // Follow the latest event; the user can still scroll up to inspect.
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length, state.run.status, state.run.stop_reason]);

  if (idle) return null;

  return (
    <section
      aria-label={t("ui.current.title")}
      className={`panel current${enlarged ? " enlarged" : ""}${collapsed ? " collapsed" : ""}`}
    >
      <div className="panel-head">
        <h2>{t("ui.current.title")}</h2>
        {/* Top control: open the larger, scrollable reading view. */}
        <button
          type="button"
          className="btn ghost"
          aria-expanded={enlarged}
          aria-label={t("ui.current.expandLarge")}
          onClick={() => setEnlarged((v) => !v)}
        >
          {enlarged ? "⤡" : "⤢"}
        </button>
      </div>
      {!collapsed && (
        <div className="audit-log" aria-live="polite" ref={logRef}>
          {events.length === 0 && (
            <div>[{state.run.run_id}] stage={state.run.current_stage || "—"} status={state.run.status || "—"}</div>
          )}
          {events.map((e, i) => (
            <div key={i} className={`ev ev-${e.kind}`}>
              {eventLine(e.kind, e.stage, e.message)}
            </div>
          ))}
          {state.run.stop_reason && <div className="ev ev-stopped">[stopped] {state.run.stop_reason}</div>}
        </div>
      )}
      {/* Bottom control: collapse/close the panel (the run keeps going). */}
      <div className="current-foot">
        <button
          type="button"
          className="btn ghost"
          aria-expanded={!collapsed}
          aria-label={t("ui.current.collapse")}
          onClick={() => setCollapsed((v) => !v)}
        >
          {collapsed ? "▴" : "▾"}
        </button>
      </div>
    </section>
  );
}

export function Workflow() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const stages = deriveWorkflow(state.run, state.runEvents);
  // Terminal success is a strict, evidence-based read of the real lifecycle
  // (see isTerminalSuccess). It only recolors completed nodes green; it never
  // fabricates progress or authorizes anything.
  const success = isTerminalSuccess(state.runEvents);
  return (
    <section aria-label={t("ui.workflow.title")} className="panel">
      <div className="panel-head"><h2>{t("ui.workflow.title")}</h2></div>
      <div className="workflow-scroll">
        <ol className="timeline" data-terminal={success ? "success" : undefined}>
          {stages.map((s) => (
            <li
              key={s.key}
              data-state={s.state}
              aria-label={`${s.label}: ${t(
                (s.state === "completed" ? "ui.stage.completed"
                  : s.state === "current" ? "ui.stage.current"
                  : s.state === "stopped" ? "ui.stage.stopped"
                  : "ui.stage.pending") as Parameters<typeof translate>[1])}`}
            >
              <span className="node" aria-hidden="true" />
              <span className="grp">{s.label}</span>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}
