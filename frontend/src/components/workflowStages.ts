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

export const RETRIEVAL_SUBSTEPS = [
  { key: "query_preparation", label: "Query Preparation" },
  { key: "candidate_retrieval", label: "Candidate Retrieval" },
  { key: "relevance_assessment", label: "Relevance Assessment" },
  { key: "candidate_identification", label: "Candidate Identification" },
  { key: "retrieval_evidence_recording", label: "Evidence Recording" },
] as const;

export type SubstepState = "pending" | "current" | "completed" | "failed" | "cancelled" | "stopped" | "unknown";

export interface RetrievalSubstepStatus {
  key: string;
  label: string;
  state: SubstepState;
}

export interface ActiveSubstepState {
  operation: string;
  phase: "started" | "completed" | "failed" | "cancelled" | "stopped" | "unknown";
  isRunning: boolean;
}

export function filterEventsByRun(events: RunEvent[], runId?: string): RunEvent[] {
  if (runId && runId.trim()) {
    // When an explicit active run_id is known, strictly isolate events to that run
    return events.filter((e) => e.run_id === runId);
  }

  // When runId is missing or omitted, do not silently merge distinct runs:
  interface RunMeta {
    runId: string;
    maxEpoch: number;
    hasInvalidTimestamp: boolean;
    count: number;
  }

  const seen = new Map<string, RunMeta>();
  for (const ev of events) {
    const rid = ev.run_id;
    if (!rid || !rid.trim()) continue;
    const ts = ev.timestamp;
    let epoch: number | null = null;
    let invalid = false;
    if (!ts || !ts.trim()) {
      invalid = true;
    } else {
      const parsed = Date.parse(ts);
      if (isNaN(parsed)) {
        invalid = true;
      } else {
        epoch = parsed;
      }
    }

    const entry = seen.get(rid);
    if (!entry) {
      seen.set(rid, {
        runId: rid,
        maxEpoch: epoch !== null ? epoch : -Infinity,
        hasInvalidTimestamp: invalid,
        count: 1,
      });
    } else {
      entry.count += 1;
      if (invalid) {
        entry.hasInvalidTimestamp = true;
      } else if (epoch !== null && epoch > entry.maxEpoch) {
        entry.maxEpoch = epoch;
      }
    }
  }

  const distinctRuns = Array.from(seen.values());
  if (distinctRuns.length === 0) {
    return events;
  }
  if (distinctRuns.length === 1) {
    // Exactly one run present: unambiguously isolate to this single run
    const singleRunId = distinctRuns[0].runId;
    return events.filter((e) => e.run_id === singleRunId);
  }

  // Multiple distinct run IDs are present without an active run_id specified.
  // We must NEVER select a run solely because its events occur later in the array.
  // We may only use timestamps when they establish an unambiguous ordering under the actual timestamp contract.

  // 1. Check timestamp validity for every distinct run:
  for (const r of distinctRuns) {
    if (r.hasInvalidTimestamp || r.maxEpoch === -Infinity) {
      // Missing or invalid timestamp on at least one event in candidate runs:
      // cannot establish unambiguous ordering reliably
      return [];
    }
  }

  // 2. Sort runs by parsed timestamp descending:
  distinctRuns.sort((a, b) => b.maxEpoch - a.maxEpoch);

  // 3. Check for equal or contradictory timestamps among top candidates:
  const topTime = distinctRuns[0].maxEpoch;
  const secondTime = distinctRuns[1].maxEpoch;
  if (topTime === secondTime) {
    // Equal or contradictory timestamps (even if formatted with different timezone offsets):
    // unambiguous latest run cannot be determined reliably
    return [];
  }

  // Unambiguous latest run established strictly by timestamp:
  const latestRunId = distinctRuns[0].runId;
  return events.filter((e) => e.run_id === latestRunId);
}

export function deriveActiveRetrievalSubstep(
  events: RunEvent[],
  runId?: string,
  isStopped?: boolean,
): ActiveSubstepState | undefined {
  const filtered = filterEventsByRun(events, runId);
  const runStopped = Boolean(
    isStopped ||
    filtered.some(
      (e) =>
        e.kind === "stopped" ||
        e.kind === "run_rejected" ||
        e.stage === "cancellation" ||
        (e as { outcome?: string }).outcome === "STOPPED"
    )
  );

  for (let i = filtered.length - 1; i >= 0; i--) {
    const ev = filtered[i];
    if (ev.stage === "retrieval" && ev.operation) {
      if (ev.phase === "completed") {
        return {
          operation: ev.operation,
          phase: "completed",
          isRunning: false,
        };
      }
      if (ev.phase === "started") {
        return {
          operation: ev.operation,
          phase: "started",
          isRunning: !runStopped,
        };
      }
      if (ev.phase === "failed" || ev.phase === "cancelled" || ev.phase === "stopped") {
        return {
          operation: ev.operation,
          phase: ev.phase,
          isRunning: false,
        };
      }
      if (!ev.phase) {
        // Explicit legacy policy: if no phase field, treat as started / running
        // unless the run has stopped, but never falsely infer completed.
        return {
          operation: ev.operation,
          phase: "started",
          isRunning: !runStopped,
        };
      }
      // Unknown / unexpected phase:
      // Must NOT be treated as started with running icon or completed.
      return {
        operation: ev.operation,
        phase: "unknown",
        isRunning: false,
      };
    }
  }
  return undefined;
}

export function deriveRetrievalSubstep(events: RunEvent[], runId?: string): string | undefined {
  const active = deriveActiveRetrievalSubstep(events, runId);
  return active ? active.operation : undefined;
}

export function deriveRetrievalSubsteps(
  stageState: TimelineState,
  events: RunEvent[],
  runId?: string,
): RetrievalSubstepStatus[] {
  if (stageState !== "current" && stageState !== "stopped" && stageState !== "completed") {
    return RETRIEVAL_SUBSTEPS.map((s) => ({
      key: s.key,
      label: s.label,
      state: "pending" as SubstepState,
    }));
  }

  const runEvents = filterEventsByRun(events, runId);

  const latestPhase = new Map<string, "started" | "completed" | "failed" | "cancelled" | "stopped" | "unknown">();
  for (const ev of runEvents) {
    if (ev.stage === "retrieval" && ev.operation) {
      if (ev.phase === "completed") {
        latestPhase.set(ev.operation, "completed");
      } else if (ev.phase === "started") {
        latestPhase.set(ev.operation, "started");
      } else if (ev.phase === "failed" || ev.phase === "cancelled" || ev.phase === "stopped") {
        latestPhase.set(ev.operation, ev.phase);
      } else if (!ev.phase) {
        // Explicit policy for unphased legacy events: record activity as started, never completed
        if (!latestPhase.has(ev.operation)) {
          latestPhase.set(ev.operation, "started");
        }
      } else {
        latestPhase.set(ev.operation, "unknown");
      }
    }
  }

  if (stageState === "completed") {
    return RETRIEVAL_SUBSTEPS.map((s) => {
      const explicit = latestPhase.get(s.key);
      let state: SubstepState = "unknown";
      if (explicit === "completed") {
        state = "completed";
      } else if (explicit === "failed") {
        state = "failed";
      } else if (explicit === "cancelled") {
        state = "cancelled";
      } else if (explicit === "stopped") {
        state = "stopped";
      } else if (explicit === "unknown") {
        state = "unknown";
      } else {
        // Started without completion, or missing event entirely: do not infer completion
        state = "unknown";
      }
      return {
        key: s.key,
        label: s.label,
        state,
      };
    });
  }

  const isRunStopped = stageState === "stopped";
  const active = deriveActiveRetrievalSubstep(runEvents, runId, isRunStopped);
  const activeOp = active?.operation;
  const activeIdx = activeOp ? RETRIEVAL_SUBSTEPS.findIndex((s) => s.key === activeOp) : -1;

  if (stageState === "stopped") {
    return RETRIEVAL_SUBSTEPS.map((s, idx) => {
      const explicit = latestPhase.get(s.key);
      let state: SubstepState = "pending";
      if (explicit === "completed") {
        state = "completed";
      } else if (explicit === "failed") {
        state = "failed";
      } else if (explicit === "cancelled") {
        state = "cancelled";
      } else if (explicit === "stopped") {
        state = "stopped";
      } else if (explicit === "started") {
        state = "stopped";
      } else if (explicit === "unknown") {
        state = "unknown";
      } else {
        const laterHadEvents = RETRIEVAL_SUBSTEPS.slice(idx + 1).some((other) => latestPhase.has(other.key));
        state = laterHadEvents ? "unknown" : "pending";
      }
      return {
        key: s.key,
        label: s.label,
        state,
      };
    });
  }

  // stageState === "current"
  return RETRIEVAL_SUBSTEPS.map((s, idx) => {
    const explicit = latestPhase.get(s.key);
    let state: SubstepState = "pending";
    if (explicit === "completed") {
      state = "completed";
    } else if (explicit === "failed") {
      state = "failed";
    } else if (explicit === "cancelled") {
      state = "cancelled";
    } else if (explicit === "stopped") {
      state = "stopped";
    } else if (explicit === "unknown") {
      state = "unknown";
    } else if (s.key === activeOp) {
      if (active?.phase === "completed") {
        state = "completed";
      } else if (active?.phase === "failed") {
        state = "failed";
      } else if (active?.phase === "cancelled") {
        state = "cancelled";
      } else if (active?.phase === "stopped") {
        state = "stopped";
      } else if (active?.phase === "unknown") {
        state = "unknown";
      } else {
        state = "current";
      }
    } else if (explicit === "started") {
      // An earlier substep that started but has no terminal event while later work is active
      // must NOT remain current, and must not be inferred as completed.
      state = "unknown";
    } else {
      // Substep with no explicit events
      if (activeIdx !== -1 && idx < activeIdx) {
        // An intermediate substep skipped without events before the active operation
        state = "unknown";
      } else {
        state = "pending";
      }
    }
    return {
      key: s.key,
      label: s.label,
      state,
    };
  });
}


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
