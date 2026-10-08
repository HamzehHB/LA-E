import { useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";
import { BACKEND_STAGE_ORDER, STAGE_GROUPS } from "./stageOrder";

function stageLabel(stage: string): string {
  const map: Record<string, string> = {
    ingestion: "Ingestion",
    retrieval: "Retrieval",
    comparison: "Comparison",
    relation: "Relation",
    core: "Core",
    classification: "Classification",
    proposal: "Proposal",
    confidence: "Confidence",
    knowledge_filter: "Knowledge Filter",
    representation: "Representation",
  };
  return map[stage] ?? stage;
}

export function CurrentAudit() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [expanded, setExpanded] = useState(false);
  const currentIdx = Math.max(0, BACKEND_STAGE_ORDER.indexOf(state.run.current_stage as (typeof BACKEND_STAGE_ORDER)[number]));

  return (
    <section aria-label={t("ui.current.title")} className="panel current">
      <div className="panel-head">
        <h2>{t("ui.current.title")}</h2>
        <span aria-live="polite">
          {state.run.run_id} · {state.run.current_stage} · {state.run.status}
        </span>
        <button type="button" aria-expanded={expanded} onClick={() => setExpanded((v) => !v)}>
          {t("ui.current.expand")}
        </button>
      </div>
      {expanded && (
        <ol className="stages">
          {BACKEND_STAGE_ORDER.map((s, i) => (
            <li
              key={s}
              data-state={i < currentIdx ? "completed" : i === currentIdx ? "current" : "pending"}
              aria-label={`${stageLabel(s)}: ${t((i < currentIdx ? "ui.stage.completed" : i === currentIdx ? "ui.stage.current" : "ui.stage.pending") as Parameters<typeof translate>[1])}`}
            >
              <span aria-hidden="true">{i < currentIdx ? "●" : i === currentIdx ? "◐" : "○"}</span> {stageLabel(s)}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function Workflow() {
  const { state } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const currentIdx = Math.max(0, BACKEND_STAGE_ORDER.indexOf(state.run.current_stage as (typeof BACKEND_STAGE_ORDER)[number]));
  return (
    <section aria-label={t("ui.workflow.title")} className="panel">
      <h2>{t("ui.workflow.title")}</h2>
      <ol className="workflow">
        {STAGE_GROUPS.map((g) => {
          const idx = g.stages.map((s) => BACKEND_STAGE_ORDER.indexOf(s));
          const done = idx.every((i) => i < currentIdx);
          const active = idx.some((i) => i === currentIdx);
          const key = done ? "ui.stage.completed" : active ? "ui.stage.current" : "ui.stage.pending";
          return (
            <li key={g.key} data-state={done ? "completed" : active ? "current" : "pending"} aria-label={`${g.key}: ${t(key as Parameters<typeof translate>[1])}`}>
              <span aria-hidden="true">{done ? "■" : active ? "◆" : "□"}</span> {g.key} <small>({g.stages.join(", ")})</small>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
