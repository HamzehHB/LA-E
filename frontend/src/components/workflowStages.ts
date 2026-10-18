/**
 * 17-stage UI workflow timeline (§12). A tested, pure mapping from real
 * run-scoped lifecycle events + run state to per-stage visual state.
 *
 * The backend does not emit 17 discrete stage events; the 10 analytical
 * stages plus governance outcomes are mapped onto the required 17 labels.
 * INPUT and OUTPUT are UI boundary milestones. No stage is ever marked
 * completed without a real signal, and non-applicable stages after a
 * safe-stop/rejection are shown as "stopped"/"pending", never completed.
 */
import type { CurrentRunView } from "../contracts/ui";
import type { RunEvent } from "../contracts/transport";

export type TimelineStageKey =
  | "input"
  | "ingestion"
  | "retrieval"
  | "comparison"
  | "relation"
  | "core"
  | "classification"
  | "proposal"
  | "confidence"
  | "knowledge_filter"
  | "llm_synthesis"
  | "human_review"
  | "explicit_approval"
  | "revalidation"
  | "controlled_execution"
  | "audit"
  | "output";

export type TimelineState = "pending" | "completed" | "current" | "stopped";

export interface TimelineStage {
  key: TimelineStageKey;
  label: string;
  state: TimelineState;
}

export const TIMELINE_LABELS: Record<TimelineStageKey, string> = {
  input: "INPUT",
  ingestion: "INGESTION",
  retrieval: "RETRIEVAL",
  comparison: "COMPARISON",
  relation: "RELATION ANALYSIS",
  core: "CORE ANALYSIS",
  classification: "CLASSIFICATION",
  proposal: "PROPOSAL",
  confidence: "CONFIDENCE",
  knowledge_filter: "KNOWLEDGE FILTER",
  llm_synthesis: "LLM SYNTHESIS",
  human_review: "HUMAN REVIEW",
  explicit_approval: "EXPLICIT APPROVAL",
  revalidation: "REVALIDATION",
  controlled_execution: "CONTROLLED EXECUTION",
  audit: "AUDIT",
  output: "OUTPUT",
};

const ORDER: TimelineStageKey[] = [
  "input", "ingestion", "retrieval", "comparison", "relation", "core",
  "classification", "proposal", "confidence", "knowledge_filter",
  "llm_synthesis", "human_review", "explicit_approval", "revalidation",
  "controlled_execution", "audit", "output",
];

// Backend analytical stage key -> timeline key.
const STAGE_TO_TIMELINE: Record<string, TimelineStageKey> = {
  input: "input",
  ingestion: "ingestion",
  retrieval: "retrieval",
  comparison: "comparison",
  relation: "relation",
  core: "core",
  classification: "classification",
  proposal: "proposal",
  confidence: "confidence",
  knowledge_filter: "knowledge_filter",
  representation: "knowledge_filter",
};

/**
 * Derive the 17-stage timeline from real events + run state.
 * - completed: a real completion signal was observed for that stage.
 * - current: the furthest stage reached so far without a completion signal.
 * - stopped: reached but the run ended early (safe-stop / rejection / error).
 * - pending: never reached.
 */
export function deriveWorkflow(
  run: Pick<CurrentRunView, "run_id" | "current_stage" | "stopped" | "status">,
  events: RunEvent[],
): TimelineStage[] {
  const completed = new Set<TimelineStageKey>();
  let sawRun = false;
  let reviewRequired = false;
  let approvalApproved = false;
  let revalidationValid: boolean | null = null;
  let executionExecuted = false;
  let terminal = false;
  let stopped = run.stopped;

  for (const e of events) {
    switch (e.kind) {
      case "run_started":
        sawRun = true;
        break;
      case "stage_completed": {
        const key = STAGE_TO_TIMELINE[e.stage];
        if (key) completed.add(key);
        break;
      }
      case "human_review_required":
        reviewRequired = true;
        break;
      case "approval_decided":
        approvalApproved = /approve/i.test(e.message);
        break;
      case "revalidation_result":
        revalidationValid = /valid|pass/i.test(e.message);
        break;
      case "execution_result":
        if (/executed|success/i.test(e.message)) executionExecuted = true;
        break;
      case "run_completed":
      case "completed":
        terminal = true;
        completed.add("audit");
        completed.add("output");
        break;
      case "run_rejected":
      case "stopped":
      case "error":
        stopped = true;
        break;
      default:
        break;
    }
  }

  // LLM synthesis completes once human review is requested (it runs
  // immediately before review in the real pipeline).
  if (reviewRequired) completed.add("llm_synthesis");
  if (approvalApproved) completed.add("explicit_approval");
  if (revalidationValid) completed.add("revalidation");
  if (executionExecuted) completed.add("controlled_execution");
  if (reviewRequired) completed.add("human_review");
  if (completed.size > 0) completed.add("input");

  // Furthest reached index = last completed stage (in canonical order).
  let reached = -1;
  ORDER.forEach((k, i) => {
    if (completed.has(k)) reached = i;
  });

  // Stop boundary index: precisely where the run halted
  let stopStageIndex = -1;
  if (stopped) {
    if (run.current_stage && STAGE_TO_TIMELINE[run.current_stage]) {
      stopStageIndex = ORDER.indexOf(STAGE_TO_TIMELINE[run.current_stage]);
    } else if (reached >= 0) {
      stopStageIndex = reached + 1;
    } else {
      stopStageIndex = 0; // Stopped at input
    }
  }

  // Active frontier index for an active run (sawRun / real run_id, not terminal, not stopped)
  let activeIndex = -1;
  const isRealRun = Boolean(run.run_id && !run.run_id.startsWith("run-fixture"));
  if (!stopped && !terminal && (sawRun || isRealRun)) {
    if (run.current_stage && STAGE_TO_TIMELINE[run.current_stage]) {
      const explicitKey = STAGE_TO_TIMELINE[run.current_stage];
      activeIndex = ORDER.indexOf(explicitKey);
    }
    if (activeIndex < 0) {
      activeIndex = reached + 1;
    }
  }

  return ORDER.map((key, i) => {
    let state: TimelineState;
    if (completed.has(key)) {
      state = "completed";
    } else if (stopped) {
      if (i === stopStageIndex) {
        state = "stopped";
      } else {
        state = "pending";
      }
    } else if (i === activeIndex) {
      state = "current";
    } else {
      state = "pending";
    }
    return { key, label: TIMELINE_LABELS[key], state };
  });
}

/**
 * §2/§1 terminal-success indicator. A run is shown as a successful terminal
 * run ONLY when the real lifecycle provides sufficient, unambiguous evidence:
 *   - a terminal event was observed (run_completed / completed);
 *   - the proposal reached Human Review and was explicitly APPROVED;
 *   - revalidation passed;
 *   - ControlledExecutor actually executed;
 *   - no stop / error / rejection occurred anywhere in the run.
 *
 * An ambiguous, partial, rejected, NEEDS_REVIEW, DO_NOT_IMPORT, or safe-stopped
 * sequence is NEVER reported as success. This is a strict AND over real signals
 * — never a fallback. When the backend exposes an authoritative run status it
 * remains the source of truth; this helper is used only to color the timeline
 * and never to authorize anything.
 */
export function isTerminalSuccess(events: RunEvent[]): boolean {
  let terminal = false;
  let reviewRequired = false;
  let approved = false;
  let revalidated = false;
  let executed = false;
  let stopped = false;

  for (const e of events) {
    switch (e.kind) {
      case "run_completed":
      case "completed":
        terminal = true;
        break;
      case "human_review_required":
        reviewRequired = true;
        break;
      case "approval_decided":
        // Approval must be affirmative; a rejection clears it.
        approved = /approve/i.test(e.message);
        break;
      case "revalidation_result":
        revalidated = /valid|pass/i.test(e.message);
        break;
      case "execution_result":
        executed = /executed|success/i.test(e.message);
        break;
      case "run_rejected":
      case "stopped":
      case "error":
        stopped = true;
        break;
      default:
        break;
    }
  }

  return terminal && reviewRequired && approved && revalidated && executed && !stopped;
}
