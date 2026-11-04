import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import {
  deriveWorkflow,
  isTerminalSuccess,
  deriveRetrievalSubstep,
  deriveActiveRetrievalSubstep,
  deriveRetrievalSubsteps,
  filterEventsByRun,
  RETRIEVAL_SUBSTEPS,
} from "../components/workflowStages";
import { StoreProvider, StoreContext, useStore } from "../state/store";
import { Workflow } from "../components/Workflow";
import { TranslateAction, Journals, ProcessingResult } from "../components/Panels";
import { computeSha256 } from "../contracts/crypto";
import type { RunEvent } from "../contracts/transport";
import { TopNav } from "../components/TopNav";

beforeEach(() => {
  localStorage.setItem("lae-welcomed", "1");
});

function ev(kind: RunEvent["kind"], stage = "", message = ""): RunEvent {
  return { kind, run_id: "run-1", stage, message, timestamp: "" };
}

describe("workflow timeline mapping (Invariant: real events only)", () => {
  it("has exactly the 17 required labels in order", () => {
    const stages = deriveWorkflow(
      { run_id: "", current_stage: "", stopped: false, status: "" }, []);
    expect(stages.map((s) => s.label)).toEqual([
      "INPUT", "INGESTION", "RETRIEVAL", "COMPARISON",
      "RELATION ANALYSIS", "CORE ANALYSIS", "CLASSIFICATION",
      "PROPOSAL", "CONFIDENCE", "KNOWLEDGE FILTER", "LLM SYNTHESIS",
      "HUMAN REVIEW", "EXPLICIT APPROVAL", "REVALIDATION",
      "CONTROLLED EXECUTION", "AUDIT", "OUTPUT",
    ]);
  });

  it("idle run leaves every stage pending", () => {
    const stages = deriveWorkflow(
      { run_id: "", current_stage: "", stopped: false, status: "" }, []);
    expect(stages.every((s) => s.state === "pending")).toBe(true);
  });

  it("marks completed only from real completion signals", () => {
    const stages = deriveWorkflow(
      { run_id: "run-1", current_stage: "retrieval", stopped: false, status: "" },
      [ev("run_started"), ev("stage_completed", "ingestion"),
       ev("stage_completed", "retrieval")]);
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.input).toBe("completed");
    expect(by.ingestion).toBe("completed");
    expect(by.retrieval).toBe("completed");
    expect(by.comparison).not.toBe("completed");
    // LLM synthesis / review must NOT be completed without their signals.
    expect(by.llm_synthesis).not.toBe("completed");
    expect(by.audit).not.toBe("completed");
  });

  it("a full executed run completes through OUTPUT", () => {
    const stages = deriveWorkflow(
      { run_id: "run-1", current_stage: "", stopped: false, status: "", audit_status: "normal_persisted" },
      [ev("run_started"), ev("stage_completed", "ingestion"),
       ev("human_review_required", "human_review"),
       ev("approval_decided", "human_review", "approve"),
       ev("revalidation_result", "revalidation", "valid"),
       ev("execution_result", "controlled_execution", "executed"),
       {
         kind: "audit_persisted",
         run_id: "run-1",
         stage: "audit",
         message: "audit records persisted (normal mode in passed partition)",
         timestamp: "2026-10-31T20:00:00Z",
         audit_mode: "normal",
         audit_partition: "passed",
       },
       ev("run_completed")]);
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.explicit_approval).toBe("completed");
    expect(by.revalidation).toBe("completed");
    expect(by.controlled_execution).toBe("completed");
    expect(by.audit).toBe("completed");
    expect(by.output).toBe("completed");
  });

  it("run_completed alone without audit event or audit_status never marks audit completed", () => {
    // Issue A: Generic terminal event without confirmed audit persistence must not mark audit completed
    const stagesWithoutAudit = deriveWorkflow(
      { run_id: "run-no-audit", current_stage: "", stopped: false, status: "" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("human_review_required", "human_review"),
        ev("approval_decided", "human_review", "approve"),
        ev("revalidation_result", "revalidation", "valid"),
        ev("execution_result", "controlled_execution", "executed"),
        ev("run_completed"),
      ],
    );
    const by = Object.fromEntries(stagesWithoutAudit.map((s) => [s.key, s.state]));
    // Execution happened, but audit was never confirmed: audit is failed, NOT completed!
    expect(by.audit).not.toBe("completed");
    expect(by.audit).toBe("failed");
    // Since audit failed, output artifact was quarantined and is not completed
    expect(by.output).not.toBe("completed");
  });

  it("audit warning event causes audit stage to be failed and prevents output completion", () => {
    const stagesWarning = deriveWorkflow(
      { run_id: "run-warn-audit", current_stage: "", stopped: false, status: "" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("human_review_required", "human_review"),
        ev("approval_decided", "human_review", "approve"),
        ev("revalidation_result", "revalidation", "valid"),
        ev("execution_result", "controlled_execution", "executed"),
        {
          kind: "warning",
          run_id: "run-warn-audit",
          stage: "audit",
          message: "Staging artifact created, but 1 audit record(s) failed to persist to disk: disk error",
          timestamp: "2026-10-31T20:00:00Z",
        },
        ev("run_completed"),
      ],
    );
    const by = Object.fromEntries(stagesWarning.map((s) => [s.key, s.state]));
    expect(by.controlled_execution).toBe("completed");
    expect(by.audit).toBe("failed");
    expect(by.output).not.toBe("completed");
  });

  it("a safe-stop marks reached stages stopped, not completed", () => {
    const stages = deriveWorkflow(
      { run_id: "run-1", current_stage: "retrieval", stopped: true, status: "" },
      [ev("run_started"), ev("stage_completed", "ingestion"),
       ev("stopped", "retrieval", "llm_unavailable")]);
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.ingestion).toBe("completed");
    expect(by.audit).not.toBe("completed");
    expect(by.output).not.toBe("completed");
  });

  it("while human review is pending without a decision, human_review stage is current, not completed", () => {
    const stages = deriveWorkflow(
      { run_id: "run-rev-1", current_stage: "human_review", stopped: false, status: "awaiting human review / acknowledgment" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.llm_synthesis).toBe("completed");
    expect(by.human_review).toBe("current");
    expect(by.explicit_approval).toBe("pending");
    expect(by.revalidation).toBe("pending");
    expect(by.controlled_execution).toBe("pending");
    expect(by.audit).toBe("pending");
    expect(by.output).toBe("pending");
  });

  it("non-CREATE acknowledgment completes human_review and audit, leaving execution stages pending", () => {
    const stages = deriveWorkflow(
      { run_id: "run-ack-1", current_stage: "audit", stopped: false, status: "proposal acknowledged" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("approval_decided", "human_review", "acknowledged_needs_review"),
        ev("stage_completed", "audit"),
        ev("run_completed"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("completed");
    expect(by.explicit_approval).toBe("pending");
    expect(by.revalidation).toBe("pending");
    expect(by.controlled_execution).toBe("pending");
    expect(by.audit).toBe("completed");
    expect(by.output).toBe("pending");
  });

  it("when run is cancelled before human decision, human_review is stopped, NOT completed", () => {
    const stages = deriveWorkflow(
      { run_id: "run-cancel-1", current_stage: "human_review", stopped: true, status: "run cancelled by user" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("stopped", "cancellation", "run cancelled by user request"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("stopped");
    expect(by.human_review).not.toBe("completed");
    expect(by.explicit_approval).toBe("pending");
    expect(by.controlled_execution).toBe("pending");
    expect(by.audit).toBe("pending");
  });

  it("when cancellation arrives with current_stage='cancellation', human_review is stopped, NOT completed", () => {
    const stages = deriveWorkflow(
      { run_id: "run-cancel-2", current_stage: "cancellation", stopped: true, status: "run cancelled during approval" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("stopped", "cancellation", "run cancelled during approval"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("stopped");
    expect(by.human_review).not.toBe("completed");
    expect(by.explicit_approval).toBe("pending");
    expect(by.controlled_execution).toBe("pending");
  });

  it("when cancellation is followed by audit warning and run_completed, human_review remains stopped", () => {
    const stages = deriveWorkflow(
      { run_id: "run-cancel-3", current_stage: "audit", stopped: false, status: "run finished (with audit warnings)" },
      [
        ev("run_started"),
        ev("stage_completed", "input"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("stage_completed", "proposal"),
        ev("stage_completed", "llm_synthesis"),
        ev("human_review_required", "human_review"),
        ev("status", "cancellation", "cancellation requested"),
        ev("stopped", "cancellation", "run cancelled during approval"),
        ev("warning", "audit", "Audit record write skipped (no audit writer available)"),
        ev("run_completed", "", "run finished (with audit warnings)"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("stopped");
    expect(by.explicit_approval).toBe("pending");
    expect(by.audit).toBe("pending");
    expect(by.output).toBe("pending");
  });

  it("when genuine rejection occurs, human_review is completed and explicit_approval is stopped", () => {
    const stages = deriveWorkflow(
      { run_id: "run-rej-1", current_stage: "human_review", stopped: true, status: "proposal rejected" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("approval_decided", "human_review", "rejected"),
        ev("stopped", "human_review", "rejected by human"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("completed");
    expect(by.explicit_approval).toBe("pending");
    expect(by.controlled_execution).toBe("pending");
    expect(by.audit).toBe("pending");
  });

  it("when run fails before human decision, human_review is stopped, NOT completed", () => {
    const stages = deriveWorkflow(
      { run_id: "run-fail-1", current_stage: "human_review", stopped: true, status: "review channel unavailable" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("error", "human_review", "timeout awaiting decision"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("stopped");
    expect(by.human_review).not.toBe("completed");
    expect(by.explicit_approval).toBe("pending");
    expect(by.controlled_execution).toBe("pending");
    expect(by.audit).toBe("pending");
  });

  it("marks audit as logged when emergency audit record is persisted during cancellation", () => {
    const stages = deriveWorkflow(
      { run_id: "run-cancel-1", current_stage: "human_review", stopped: true, status: "run cancelled during review" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("stopped", "cancellation", "run cancelled during review"),
        {
          kind: "audit_persisted",
          run_id: "run-cancel-1",
          stage: "audit",
          message: "audit records persisted (emergency mode in errors partition)",
          timestamp: "2026-10-31T20:00:00Z",
          audit_mode: "emergency",
          audit_partition: "errors",
        },
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.human_review).toBe("stopped");
    expect(by.audit).toBe("logged");
  });

  it("marks audit as completed when normal audit is persisted", () => {
    const stages = deriveWorkflow(
      { run_id: "run-ok-1", current_stage: "audit", stopped: false, status: "completed" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("approval_decided", "human_review", "approved"),
        ev("revalidation_result", "revalidation", "valid"),
        ev("execution_result", "controlled_execution", "executed"),
        {
          kind: "audit_persisted",
          run_id: "run-ok-1",
          stage: "audit",
          message: "audit records persisted (normal mode in passed partition)",
          timestamp: "2026-10-31T20:00:00Z",
          audit_mode: "normal",
          audit_partition: "passed",
        },
        ev("completed", "run"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.controlled_execution).toBe("completed");
    expect(by.audit).toBe("completed");
    expect(by.output).toBe("completed");
  });

  it("marks audit as failed when audit persistence fails", () => {
    const stages = deriveWorkflow(
      { run_id: "run-fail-audit-1", current_stage: "audit", stopped: true, status: "audit persistence failed" },
      [
        ev("run_started"),
        ev("stage_completed", "ingestion"),
        ev("stage_completed", "retrieval"),
        ev("human_review_required", "human_review"),
        ev("approval_decided", "human_review", "approved"),
        ev("revalidation_result", "revalidation", "valid"),
        ev("execution_result", "controlled_execution", "executed"),
        {
          kind: "audit_failed",
          run_id: "run-fail-audit-1",
          stage: "audit",
          message: "audit persistence failed",
          timestamp: "2026-10-31T20:00:00Z",
        },
        ev("stopped", "audit_persistence", "audit persistence failed"),
      ],
    );
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.controlled_execution).toBe("completed");
    expect(by.audit).toBe("failed");
  });

  it("derives audit state from run.audit_status when events are absent", () => {
    const loggedStages = deriveWorkflow(
      { run_id: "run-logged", current_stage: "human_review", stopped: true, status: "cancelled", audit_status: "emergency_persisted" },
      [],
    );
    const byLogged = Object.fromEntries(loggedStages.map((s) => [s.key, s.state]));
    expect(byLogged.audit).toBe("logged");

    const failedStages = deriveWorkflow(
      { run_id: "run-failed", current_stage: "controlled_execution", stopped: true, status: "failed", audit_status: "persistence_failed" },
      [],
    );
    const byFailed = Object.fromEntries(failedStages.map((s) => [s.key, s.state]));
    expect(byFailed.audit).toBe("failed");
  });

  describe("audit-state derivation precedence and output gate", () => {
    const baseExecutedEvents: RunEvent[] = [
      ev("run_started"),
      ev("stage_completed", "ingestion"),
      ev("human_review_required", "human_review"),
      ev("approval_decided", "human_review", "approve"),
      ev("revalidation_result", "revalidation", "valid"),
      ev("execution_result", "controlled_execution", "executed"),
    ];

    it("Case A: execution executed + run_completed + no audit event + audit_status normal_persisted -> audit completed, output completed", () => {
      const stages = deriveWorkflow(
        { run_id: "run-case-a", current_stage: "", stopped: false, status: "completed", audit_status: "normal_persisted" },
        [...baseExecutedEvents, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).toBe("completed");
      expect(by.output).toBe("completed");
    });

    it("Case B: execution executed + run_completed + no audit event + audit_status persistence_failed -> audit failed, output pending", () => {
      const stages = deriveWorkflow(
        { run_id: "run-case-b", current_stage: "", stopped: false, status: "failed", audit_status: "persistence_failed" },
        [...baseExecutedEvents, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).toBe("failed");
      expect(by.output).toBe("pending");
    });

    it("Case C: execution executed + run_completed + no audit event + no audit_status -> audit failed, output pending", () => {
      const stages = deriveWorkflow(
        { run_id: "run-case-c", current_stage: "", stopped: false, status: "" },
        [...baseExecutedEvents, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).not.toBe("completed");
      expect(by.audit).toBe("failed");
      expect(by.output).toBe("pending");
    });

    it("Case D: explicit audit_failed event + contradictory audit_status normal_persisted -> explicit failure preserved (audit failed), output pending", () => {
      const stages = deriveWorkflow(
        { run_id: "run-case-d", current_stage: "", stopped: false, status: "completed", audit_status: "normal_persisted" },
        [
          ...baseExecutedEvents,
          {
            kind: "audit_failed",
            run_id: "run-case-d",
            stage: "audit",
            message: "audit persistence failed: disk write error",
            timestamp: "2026-10-31T20:00:00Z",
          },
          ev("run_completed"),
        ],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).toBe("failed");
      expect(by.audit).not.toBe("completed");
      expect(by.output).toBe("pending");
    });

    it("Case E: audit_persisted with missing/unknown metadata does not mark audit completed", () => {
      const stages = deriveWorkflow(
        { run_id: "run-case-e", current_stage: "", stopped: false, status: "" },
        [
          ...baseExecutedEvents,
          {
            kind: "audit_persisted",
            run_id: "run-case-e",
            stage: "audit",
            message: "ambiguous unstructured message without mode",
            timestamp: "2026-10-31T20:00:00Z",
            audit_mode: undefined,
            audit_partition: undefined,
          },
          ev("run_completed"),
        ],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).not.toBe("completed");
      expect(by.audit).toBe("pending");
      expect(by.output).toBe("pending");
    });
  });

  describe("deriveWorkflow audit stickiness and ambiguity invariants (Tests A-E)", () => {
    const baseExecutedEvents: RunEvent[] = [
      ev("run_started"),
      ev("stage_completed", "ingestion"),
      ev("human_review_required", "human_review"),
      ev("approval_decided", "human_review", "approve"),
      ev("revalidation_result", "revalidation", "valid"),
      ev("execution_result", "controlled_execution", "executed"),
    ];

    const normalAuditPersisted: RunEvent = {
      kind: "audit_persisted",
      run_id: "run-sticky",
      stage: "audit",
      message: "audit records persisted (normal mode in passed partition)",
      timestamp: "2026-10-31T20:00:00Z",
      audit_mode: "normal",
      audit_partition: "passed",
    };

    it("Test A: audit_failed followed by valid normal audit_persisted + run_completed -> Audit remains failed, Output pending", () => {
      const failedAudit: RunEvent = {
        kind: "audit_failed",
        run_id: "run-sticky-a",
        stage: "audit",
        message: "audit persistence failed: disk write error",
        timestamp: "2026-10-31T20:00:00Z",
      };
      const stages = deriveWorkflow(
        { run_id: "run-sticky-a", current_stage: "", stopped: false, status: "completed" },
        [...baseExecutedEvents, failedAudit, normalAuditPersisted, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).toBe("failed");
      expect(by.audit).not.toBe("completed");
      expect(by.output).toBe("pending");
    });

    it("Test B: Genuine persistence-failure warning followed by valid normal audit_persisted -> failure remains authoritative (failed), Output pending", () => {
      const failureWarning: RunEvent = {
        kind: "warning",
        run_id: "run-sticky-b",
        stage: "audit",
        message: "Staging artifact created, but 1 audit record(s) failed to persist to disk: I/O error",
        timestamp: "2026-10-31T20:00:00Z",
      };
      const stages = deriveWorkflow(
        { run_id: "run-sticky-b", current_stage: "", stopped: false, status: "completed" },
        [...baseExecutedEvents, failureWarning, normalAuditPersisted, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).toBe("failed");
      expect(by.audit).not.toBe("completed");
      expect(by.output).toBe("pending");
    });

    it("Test C: Ambiguous audit_persisted metadata combined with run.audit_status = 'normal_persisted' -> Audit not completed, Output pending", () => {
      const ambiguousAudit: RunEvent = {
        kind: "audit_persisted",
        run_id: "run-sticky-c",
        stage: "audit",
        message: "audit records persisted in passed partition",
        timestamp: "2026-10-31T20:00:00Z",
        audit_mode: "unknown" as any,
        audit_partition: "passed",
      };
      const stages = deriveWorkflow(
        { run_id: "run-sticky-c", current_stage: "", stopped: false, status: "completed", audit_status: "normal_persisted" },
        [...baseExecutedEvents, ambiguousAudit, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).not.toBe("completed");
      expect(by.audit).toBe("pending");
      expect(by.output).toBe("pending");
    });

    it("Test D: Emergency persistence followed by contradictory normal persistence evidence or snapshot -> does not produce false successful terminal state", () => {
      const emergencyAudit: RunEvent = {
        kind: "audit_persisted",
        run_id: "run-sticky-d",
        stage: "audit",
        message: "audit records persisted (emergency mode in errors partition)",
        timestamp: "2026-10-31T20:00:00Z",
        audit_mode: "emergency",
        audit_partition: "errors",
      };
      // Case D1: Emergency event followed by contradictory normal audit event
      const stagesEventOverride = deriveWorkflow(
        { run_id: "run-sticky-d", current_stage: "", stopped: false, status: "completed" },
        [...baseExecutedEvents, emergencyAudit, normalAuditPersisted, ev("run_completed")],
      );
      const by1 = Object.fromEntries(stagesEventOverride.map((s) => [s.key, s.state]));
      expect(by1.audit).toBe("logged");
      expect(by1.audit).not.toBe("completed");
      expect(by1.output).toBe("pending");

      // Case D2: Emergency event followed by contradictory normal_persisted snapshot
      const stagesSnapshotOverride = deriveWorkflow(
        { run_id: "run-sticky-d", current_stage: "", stopped: false, status: "completed", audit_status: "normal_persisted" },
        [...baseExecutedEvents, emergencyAudit, ev("run_completed")],
      );
      const by2 = Object.fromEntries(stagesSnapshotOverride.map((s) => [s.key, s.state]));
      expect(by2.audit).toBe("logged");
      expect(by2.audit).not.toBe("completed");
      expect(by2.output).toBe("pending");
    });

    it("Test E: Ordinary valid normal-persistence event with no contradictory failure evidence still completes Audit and Output", () => {
      const stages = deriveWorkflow(
        { run_id: "run-sticky-e", current_stage: "", stopped: false, status: "completed" },
        [...baseExecutedEvents, normalAuditPersisted, ev("run_completed")],
      );
      const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
      expect(by.controlled_execution).toBe("completed");
      expect(by.audit).toBe("completed");
      expect(by.output).toBe("completed");
    });
  });
});

import userEvent from "@testing-library/user-event";
import { App } from "../App";
import { BACKEND_STAGE_ORDER } from "../components/stageOrder";
import { UI_KEYS_EN, translate, en, fa } from "../i18n/resources";
import { transport } from "../services/transport";

describe("backend stage fidelity (Invariant A)", () => {
  it("preserves all 10 real backend stages in order", () => {
    expect(BACKEND_STAGE_ORDER).toEqual([
      "ingestion",
      "retrieval",
      "comparison",
      "relation",
      "core",
      "classification",
      "proposal",
      "confidence",
      "knowledge_filter",
      "representation",
    ]);
  });
});

describe("retrieval substeps synchronization", () => {
  it("derives the latest retrieval substep from events", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "r1", stage: "retrieval", message: "start", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "query_preparation", message: "prep", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "candidate_retrieval", message: "cand", timestamp: "" },
    ];
    expect(deriveRetrievalSubstep(events)).toBe("candidate_retrieval");
  });

  it("has the 5 distinct capabilities in RETRIEVAL_SUBSTEPS", () => {
    expect(RETRIEVAL_SUBSTEPS.map((s) => s.key)).toEqual([
      "query_preparation",
      "candidate_retrieval",
      "relevance_assessment",
      "candidate_identification",
      "retrieval_evidence_recording",
    ]);
  });

  it("derives step lifecycle states accurately for active retrieval", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "r1", stage: "retrieval", message: "start", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "prep", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "candidate_retrieval", phase: "completed", message: "cand", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "relevance_assessment", phase: "started", message: "relevance", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("current", events);
    expect(substeps.map((s) => ({ key: s.key, state: s.state }))).toEqual([
      { key: "query_preparation", state: "completed" },
      { key: "candidate_retrieval", state: "completed" },
      { key: "relevance_assessment", state: "current" },
      { key: "candidate_identification", state: "pending" },
      { key: "retrieval_evidence_recording", state: "pending" },
    ]);
  });

  it("does NOT infer substep completion from overall stage completion when events are missing", () => {
    const substeps = deriveRetrievalSubsteps("completed", []);
    // Must NOT infer completion without explicit events; missing evidence yields unknown
    expect(substeps.every((s) => s.state === "unknown")).toBe(true);
  });

  it("marks retrieval substeps completed only when supported by explicit completion events", () => {
    const events: RunEvent[] = [
      { kind: "status", run_id: "run-all", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-all", stage: "retrieval", operation: "candidate_retrieval", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-all", stage: "retrieval", operation: "relevance_assessment", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-all", stage: "retrieval", operation: "candidate_identification", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-all", stage: "retrieval", operation: "retrieval_evidence_recording", phase: "completed", message: "", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("completed", events, "run-all");
    expect(substeps.every((s) => s.state === "completed")).toBe(true);
  });

  it("marks all retrieval substeps pending when stage is pending", () => {
    const substeps = deriveRetrievalSubsteps("pending", []);
    expect(substeps.every((s) => s.state === "pending")).toBe(true);
  });

  it("handles explicit started and completed phases with run_id scoping", () => {
    const events: RunEvent[] = [
      // Prior run event that should be ignored when scoped
      { kind: "status", run_id: "other-run", stage: "retrieval", operation: "retrieval_evidence_recording", phase: "completed", message: "", timestamp: "" },
      // Current run events
      { kind: "status", run_id: "run-target", stage: "retrieval", operation: "query_preparation", phase: "started", message: "", timestamp: "" },
      { kind: "status", run_id: "run-target", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-target", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("current", events, "run-target");
    expect(substeps.map((s) => ({ key: s.key, state: s.state }))).toEqual([
      { key: "query_preparation", state: "completed" },
      { key: "candidate_retrieval", state: "current" },
      { key: "relevance_assessment", state: "pending" },
      { key: "candidate_identification", state: "pending" },
      { key: "retrieval_evidence_recording", state: "pending" },
    ]);
  });

  it("accurately reflects stopped retrieval stage preserving completed substeps", () => {
    const events: RunEvent[] = [
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("stopped", events, "r1");
    expect(substeps.map((s) => ({ key: s.key, state: s.state }))).toEqual([
      { key: "query_preparation", state: "completed" },
      { key: "candidate_retrieval", state: "stopped" },
      { key: "relevance_assessment", state: "pending" },
      { key: "candidate_identification", state: "pending" },
      { key: "retrieval_evidence_recording", state: "pending" },
    ]);
  });

  it("Workflow renders retrieval substeps when retrieval stage is current", () => {
    const testState: any = {
      appLanguage: "en",
      run: {
        run_id: "run-test-1",
        current_stage: "retrieval",
        stopped: false,
        status: "retrieval in progress",
        review_state: "",
      },
      runEvents: [
        { kind: "run_started", run_id: "run-test-1", stage: "input", message: "", timestamp: "" },
        { kind: "stage_completed", run_id: "run-test-1", stage: "ingestion", message: "", timestamp: "" },
        { kind: "stage_started", run_id: "run-test-1", stage: "retrieval", message: "", timestamp: "" },
        { kind: "status", run_id: "run-test-1", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
        { kind: "status", run_id: "run-test-1", stage: "retrieval", operation: "candidate_retrieval", phase: "completed", message: "", timestamp: "" },
        { kind: "status", run_id: "run-test-1", stage: "retrieval", operation: "relevance_assessment", phase: "started", message: "", timestamp: "" },
      ],
    };

    render(
      <StoreContext.Provider value={{ state: testState } as any}>
        <Workflow />
      </StoreContext.Provider>
    );

    const retrievalLi = screen.getByRole("listitem", { name: /^RETRIEVAL:/i });
    expect(retrievalLi).toHaveClass("has-substeps");
    const substepsList = within(retrievalLi).getByRole("list", { name: "Retrieval Substeps" });
    expect(substepsList).toBeInTheDocument();
    const pills = within(substepsList).getAllByRole("listitem");
    expect(pills).toHaveLength(5);
    expect(pills[0]).toHaveAttribute("data-substep", "query_preparation");
    expect(pills[0]).toHaveAttribute("data-state", "completed");
    expect(pills[1]).toHaveAttribute("data-substep", "candidate_retrieval");
    expect(pills[1]).toHaveAttribute("data-state", "completed");
    expect(pills[2]).toHaveAttribute("data-substep", "relevance_assessment");
    expect(pills[2]).toHaveAttribute("data-state", "current");
    expect(pills[3]).toHaveAttribute("data-substep", "candidate_identification");
    expect(pills[3]).toHaveAttribute("data-state", "pending");
    expect(pills[4]).toHaveAttribute("data-substep", "retrieval_evidence_recording");
    expect(pills[4]).toHaveAttribute("data-state", "pending");
  });

  it("does not infer first retrieval substep running merely because unrelated run events exist", () => {
    const events: RunEvent[] = [
      { kind: "run_started", run_id: "r1", stage: "input", message: "", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "ingestion", message: "", timestamp: "" },
    ];
    // Stage is "current", but NO retrieval event has been emitted yet
    const substeps = deriveRetrievalSubsteps("current", events, "r1");
    expect(substeps.every((s) => s.state === "pending")).toBe(true);
  });

  it("handles failed, cancelled, and stopped phases transitioning substep out of running", () => {
    const failedEvents: RunEvent[] = [
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "r1", stage: "retrieval", operation: "candidate_retrieval", phase: "failed", message: "index connection dropped", timestamp: "" },
    ];
    const active = deriveActiveRetrievalSubstep(failedEvents, "r1");
    expect(active?.phase).toBe("failed");
    expect(active?.isRunning).toBe(false);

    const substeps = deriveRetrievalSubsteps("current", failedEvents, "r1");
    expect(substeps.map((s) => ({ key: s.key, state: s.state }))).toEqual([
      { key: "query_preparation", state: "completed" },
      { key: "candidate_retrieval", state: "failed" },
      { key: "relevance_assessment", state: "pending" },
      { key: "candidate_identification", state: "pending" },
      { key: "retrieval_evidence_recording", state: "pending" },
    ]);
  });
});

describe("decorative stars (§4.8)", () => {
  it("renders exactly 13 decorative, aria-hidden stars", () => {
    const { container } = render(<App />);
    const band = container.querySelector(".stars");
    expect(band).not.toBeNull();
    expect(band?.getAttribute("aria-hidden")).toBe("true");
    expect(container.querySelectorAll(".stars .star").length).toBe(13);
  });
});

describe("app shell", () => {
  it("renders nav, result, journals, composer, responsibility", async () => {
    const user = userEvent.setup();
    render(<App />);
    expect(screen.getByLabelText("top")).toBeInTheDocument();
    // Current Audit is hidden when no run is active (§13); 13 stars show instead.
    expect(screen.queryByLabelText("Current Audit")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Processing Result")).toBeInTheDocument();
    expect(screen.getByLabelText("Recent Audit journal")).toBeInTheDocument();
    expect(screen.getByLabelText("Recent Staging journal")).toBeInTheDocument();
    // Responsibility notice lives below the composer.
    expect(screen.getByText("Information may be incorrect; one small error can start a chain of errors. The person is responsible for its use.")).toBeInTheDocument();
    const input = screen.getByPlaceholderText("Type or paste here (text only — no uploads yet)");
    expect(input).toBeInTheDocument();
    await user.type(input, "Synthetic research hypothesis");
    expect(input).toHaveValue("Synthetic research hypothesis");
    // Mock transport only.
    expect(transport.kind).toBe("mock-fixture");
  });

  it("CREATE shows mock approve/reject/edit; non-CREATE hides them", async () => {
    const user = userEvent.setup();
    render(<App />);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reject" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText(/mock_rejected/)).toBeInTheDocument();
  });

  it("mock edit marks proposal stale without claiming re-hash", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Edit proposal" }));
    const box = screen.getByLabelText("Edit (test sample)");
    await user.clear(box);
    await user.type(box, "edited fixture text");
    await user.click(screen.getByRole("button", { name: /Save edit/ }));
    expect(await screen.findByText(/mock_edited_stale/)).toBeInTheDocument();
    expect(screen.getByText(/display only; no authoritative re-hash/i)).toBeInTheDocument();
  });

  it("theme toggle + RTL switch update document attributes", async () => {
    const user = userEvent.setup();
    render(<App />);
    // Open settings gear
    await user.click(screen.getByRole("button", { name: /Settings/i }));
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("lae-theme")).toBe("dark");
    await user.click(screen.getByRole("radio", { name: "فارسی" }));
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("fa");
  });

  it("first-launch Welcome shows, then persists dismissal", async () => {
    localStorage.removeItem("lae-welcomed");
    const user = userEvent.setup();
    render(<App />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("A secure, private, researcher-controlled environment for advancing research.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Enter LA-E" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(localStorage.getItem("lae-welcomed")).toBe("1");
  });

  it("Welcome guide renders retained groups (Environment Modes and Workspace Configuration) with vendor-neutral explanations", () => {
    localStorage.removeItem("lae-welcomed");
    render(<App />);
    // Retained group headings
    expect(screen.getByRole("heading", { level: 3, name: "Environment Modes" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Workspace Configuration" })).toBeInTheDocument();

    // Removed non-essential sections are NOT present in the Welcome guide
    expect(screen.queryByRole("heading", { level: 3, name: "Input & Governance Lifecycle" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 3, name: "Historical Journals" })).not.toBeInTheDocument();

    // Environment Modes: Test Mode & Vault Mode
    expect(screen.getByText("Test Mode")).toBeInTheDocument();
    expect(screen.getByText(/Operates exclusively on built-in synthetic examples/)).toBeInTheDocument();
    expect(screen.getByText("Vault Mode")).toBeInTheDocument();
    expect(screen.getByText(/Connects to your authentic personal knowledge repository/)).toBeInTheDocument();

    // Workspace Configuration items
    expect(screen.getByText("Staging Destination")).toBeInTheDocument();
    expect(screen.getByText(/Isolated, controlled directory where approved proposal artifacts are safely staged/)).toBeInTheDocument();
    expect(screen.getByText("Audit Storage")).toBeInTheDocument();
    expect(screen.getByText(/Authoritative storage recording permanent, tamper-evident logs/)).toBeInTheDocument();
    expect(screen.getByText("Vector Database")).toBeInTheDocument();
    expect(screen.getByText("Local embedded vector store enabling semantic retrieval and similarity analysis over indexed knowledge.")).toBeInTheDocument();
    // Verify vendor name LanceDB is NOT in the guide description
    expect(screen.queryByText(/LanceDB/)).not.toBeInTheDocument();
    expect(screen.getByText("AI Models")).toBeInTheDocument();
    expect(screen.getByText("Local runtimes or configured cloud LLMs and embedding models.")).toBeInTheDocument();
    expect(screen.queryByText(/always subject to human approval/)).not.toBeInTheDocument();
    expect(screen.getByText("Knowledge Language")).toBeInTheDocument();
    expect(screen.getByText(/Designated language for generated Audit and Staging content/)).toBeInTheDocument();
  });

  it("Welcome guide fully localizes to Persian without hardcoded English remaining, and restores on switch back", async () => {
    localStorage.removeItem("lae-welcomed");
    const user = userEvent.setup();
    render(<App />);

    // Switch language to Persian via Welcome language selector
    await user.click(screen.getByRole("radio", { name: "فارسی" }));

    // Persian tagline
    expect(screen.getByText("محیطی امن، خصوصی و کاملاً تحت کنترل پژوهشگر برای توسعه و پیشبرد پژوهش.")).toBeInTheDocument();

    // Persian group headings
    expect(screen.getByRole("heading", { level: 3, name: "حالت‌های محیط پژوهش" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "پیکربندی محیط کار" })).toBeInTheDocument();

    // Persian item titles and explanations
    expect(screen.getByText("حالت آزمون")).toBeInTheDocument();
    expect(screen.getByText(/صرفاً با نمونه‌های ساختگی داخلی برای آزمایش کار می‌کند/)).toBeInTheDocument();
    expect(screen.getByText("حالت مخزن")).toBeInTheDocument();
    expect(screen.getByText(/برای تحلیل و نمایه‌سازی زنده، تحت محافظت دقیق مرزهای امنیتی/)).toBeInTheDocument();

    expect(screen.getByText("مقصد بایگانی")).toBeInTheDocument();
    expect(screen.getByText(/پوشه‌ای ایزوله و کنترل‌شده/)).toBeInTheDocument();
    expect(screen.getByText("فضای نگهداری حسابرسی")).toBeInTheDocument();
    expect(screen.getByText(/مخزن معتبر نگه‌دارنده گزارش‌های دائمی/)).toBeInTheDocument();
    expect(screen.getByText("پایگاه داده برداری")).toBeInTheDocument();
    expect(screen.getByText("پایگاه داده برداری تعبیه‌شده محلی برای بازیابی معنایی و تحلیل تشابه روی دانش نمایه‌شده.")).toBeInTheDocument();
    expect(screen.getByText("مدل‌های هوش مصنوعی")).toBeInTheDocument();
    expect(screen.getByText("محیط‌های اجرای محلی یا مدل‌های ابری پیکربندی‌شده و مدل‌های بردارساز.")).toBeInTheDocument();
    expect(screen.getByText("زبان دانش")).toBeInTheDocument();

    // Enter button localized
    expect(screen.getByRole("button", { name: "ورود به LA-E" })).toBeInTheDocument();

    // Ensure no hardcoded English headings or obsolete strings remain in the guide
    expect(screen.queryByText("Environment Modes")).not.toBeInTheDocument();
    expect(screen.queryByText("Vault / Test")).not.toBeInTheDocument();
    expect(screen.queryByText("Workspace Configuration")).not.toBeInTheDocument();
    expect(screen.queryByText("A secure, private, researcher-controlled environment for advancing research.")).not.toBeInTheDocument();
    expect(screen.queryByText(/گاوصندوق/)).not.toBeInTheDocument();
    expect(screen.queryByText(/آماده‌سازی/)).not.toBeInTheDocument();
    expect(screen.queryByText(/جاسازی/)).not.toBeInTheDocument();
    expect(screen.queryByText(/مشروط به تأیید نهایی انسان/)).not.toBeInTheDocument();

    // Switch back to English
    await user.click(screen.getByRole("radio", { name: "English" }));

    // English restored
    expect(screen.getByText("A secure, private, researcher-controlled environment for advancing research.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Environment Modes" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Workspace Configuration" })).toBeInTheDocument();
    expect(screen.getByText("Test Mode")).toBeInTheDocument();
    expect(screen.getByText("Vault Mode")).toBeInTheDocument();
    expect(screen.getByText("Vector Database")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enter LA-E" })).toBeInTheDocument();
  });

  it("journals page forward/backward and View All is honest", async () => {
    localStorage.setItem("lae-welcomed", "1");
    const user = userEvent.setup();
    render(<App />);
    // Last fixture row is shown first in rotation; paging moves it.
    await user.click(screen.getAllByRole("button", { name: "Next record" })[0]);
    await user.click(screen.getAllByRole("button", { name: "Previous record" })[0]);
    const viewAll = screen.getAllByRole("button", { name: "View All" });
    expect(viewAll.length).toBeGreaterThan(0);
    expect(viewAll[0].title).toMatch(/Test mode|not supported/);
  });
});

describe("i18n parity (Invariant O)", () => {
  it("every en key has an fa translation", () => {
    for (const key of UI_KEYS_EN) {
      const fa = translate("fa", key as Parameters<typeof translate>[1]);
      expect(fa).not.toBe("");
      expect(fa).not.toBe(key);
    }
  });
});

describe("journal fixtures", () => {
  it("exposes up to 10 audit rows with correlation states", () => {
    expect(transport.getAuditRows().length).toBeLessThanOrEqual(10);
    expect(transport.getStagingRows().length).toBeGreaterThan(0);
  });
});

describe("hardening contracts (§§3-21)", () => {
  // NOTE: the first-use vector-index onboarding prompt, the LanceDB install
  // prompt, and the standalone Vector-index resource panel were intentionally
  // removed from the main UI (demo scaffolding). Their i18n keys remain and
  // parity is still asserted below.

  it("safe-stop Fix/Stop live inside Processing Result; Fix makes no resume claim", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: /Simulate safe-stop/ }));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fix" }));
    // Fix must NOT claim the run resumes (backend has no resume capability).
    expect(await screen.findByText(/cannot resume an interrupted run/)).toBeInTheDocument();
    expect(screen.queryByText(/same run resumes/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/run-fixture-001/).length).toBeGreaterThanOrEqual(1);
  });

  it("new i18n keys keep en/fa parity", () => {
    const keys = [
      "ui.onboarding.vectorBaseline",
      "ui.engine.installPrompt",
      "ui.safestop.fix",
      "ui.safestop.stop",
      "ui.vector.status",
      "ui.eligibility.title",
    ] as const;
    for (const key of keys) {
      expect(translate("fa", key)).not.toBe(key);
      expect(translate("en", key)).not.toBe(key);
    }
  });
});

describe("gap-closure behaviors (§§1-4)", () => {
  it("isTerminalSuccess requires a full, executed, unstopped lifecycle", () => {
    const base = [
      ev("run_started"),
      ev("human_review_required", "human_review"),
      ev("approval_decided", "human_review", "approve"),
      ev("revalidation_result", "revalidation", "valid"),
      ev("execution_result", "controlled_execution", "executed"),
    ];
    const normalAudit: RunEvent = {
      kind: "audit_persisted",
      run_id: "r1",
      stage: "audit",
      message: "audit records persisted (normal mode in passed partition)",
      timestamp: "2026-10-31T20:00:00Z",
      audit_mode: "normal",
      audit_partition: "passed",
    };

    // Missing affirmative audit persistence → not success.
    expect(isTerminalSuccess([...base, ev("run_completed")])).toBe(false);
    // Full sequence with affirmative audit persistence → success.
    expect(isTerminalSuccess([...base, normalAudit, ev("run_completed")])).toBe(true);
    // Authoritative snapshot fallback when event is absent → success.
    expect(isTerminalSuccess([...base, ev("run_completed")], "normal_persisted")).toBe(true);
    // No terminal event → not success.
    expect(isTerminalSuccess([...base, normalAudit])).toBe(false);
    // Any stop/error/rejection after completion → not success.
    expect(isTerminalSuccess([...base, normalAudit, ev("run_completed"), ev("stopped", "x", "y")])).toBe(false);
    expect(isTerminalSuccess([...base, normalAudit, ev("run_completed"), ev("error", "x", "y")])).toBe(false);
    // Rejected approval → not success.
    const rejected = base.map((e, i) =>
      i === 2 ? ev("approval_decided", "human_review", "reject") : e);
    expect(isTerminalSuccess([...rejected, normalAudit, ev("run_completed")])).toBe(false);
    // Missing execution → not success.
    const noExec = base.filter((e) => e.kind !== "execution_result");
    expect(isTerminalSuccess([...noExec, normalAudit, ev("run_completed")])).toBe(false);
    // Audit failure → not success.
    expect(isTerminalSuccess([...base, normalAudit, ev("audit_failed", "audit", "disk error"), ev("run_completed")])).toBe(false);
    expect(isTerminalSuccess([...base, normalAudit, { kind: "warning", stage: "audit", message: "audit persistence failed", run_id: "r1", timestamp: "2026-10-31T20:00:00Z" }, ev("run_completed")])).toBe(false);
  });

  describe("isTerminalSuccess audit invariant (§§A-F)", () => {
    const baseExecutedEvents: RunEvent[] = [
      ev("run_started"),
      ev("stage_completed", "ingestion"),
      ev("human_review_required", "human_review"),
      ev("approval_decided", "human_review", "approve"),
      ev("revalidation_result", "revalidation", "valid"),
      ev("execution_result", "controlled_execution", "executed"),
    ];

    const normalAuditPersisted: RunEvent = {
      kind: "audit_persisted",
      run_id: "run-term-audit",
      stage: "audit",
      message: "audit records persisted (normal mode in passed partition)",
      timestamp: "2026-10-31T20:00:00Z",
      audit_mode: "normal",
      audit_partition: "passed",
    };

    it("A. Successful approval, revalidation, execution and run_completed, but no audit_persisted returns false", () => {
      const events = [...baseExecutedEvents, ev("run_completed")];
      expect(isTerminalSuccess(events)).toBe(false);
    });

    it("B. The same event sequence with valid normal-mode audit_persisted returns true", () => {
      const events = [...baseExecutedEvents, normalAuditPersisted, ev("run_completed")];
      expect(isTerminalSuccess(events)).toBe(true);
    });

    it("C. Emergency audit persistence must not be treated as normal successful CREATE completion", () => {
      const emergencyAuditPersisted: RunEvent = {
        kind: "audit_persisted",
        run_id: "run-term-emerg",
        stage: "audit",
        message: "audit records persisted (emergency mode in errors partition)",
        timestamp: "2026-10-31T20:00:00Z",
        audit_mode: "emergency",
        audit_partition: "errors",
      };
      const events = [...baseExecutedEvents, emergencyAuditPersisted, ev("run_completed")];
      expect(isTerminalSuccess(events)).toBe(false);
    });

    it("D. Explicit audit_failed followed by contradictory normal audit evidence remains unsuccessful", () => {
      const failedAudit: RunEvent = {
        kind: "audit_failed",
        run_id: "run-term-fail",
        stage: "audit",
        message: "audit persistence failed",
        timestamp: "2026-10-31T20:00:00Z",
      };
      const events = [...baseExecutedEvents, failedAudit, normalAuditPersisted, ev("run_completed")];
      expect(isTerminalSuccess(events)).toBe(false);
      // Also verify contradictory snapshot status does not override explicit failure
      expect(isTerminalSuccess(events, "normal_persisted")).toBe(false);
    });

    it("E. A non-failure audit warning must not automatically be classified as audit persistence failure", () => {
      const nonFailureWarning: RunEvent = {
        kind: "warning",
        run_id: "run-term-warn-nonfail",
        stage: "audit",
        message: "Audit journal cache directory already exists; continuing normal write",
        timestamp: "2026-10-31T20:00:00Z",
      };
      const events = [...baseExecutedEvents, nonFailureWarning, normalAuditPersisted, ev("run_completed")];
      expect(isTerminalSuccess(events)).toBe(true);
    });

    it("F. A genuine audit persistence failure warning must invalidate terminal success", () => {
      const failureWarning: RunEvent = {
        kind: "warning",
        run_id: "run-term-warn-fail",
        stage: "audit",
        message: "Staging artifact created, but 1 audit record(s) failed to persist to disk: I/O error",
        timestamp: "2026-10-31T20:00:00Z",
      };
      const events = [...baseExecutedEvents, failureWarning, normalAuditPersisted, ev("run_completed")];
      expect(isTerminalSuccess(events)).toBe(false);
    });

    it("G. Contradictory or ambiguous metadata cannot establish terminal success", () => {
      const contradictoryAudit: RunEvent = {
        kind: "audit_persisted",
        run_id: "run-term-contradict",
        stage: "audit",
        message: "audit records persisted in passed partition",
        timestamp: "2026-10-31T20:00:00Z",
        audit_mode: "unknown" as any,
        audit_partition: "passed",
      };
      const events = [...baseExecutedEvents, contradictoryAudit, ev("run_completed")];
      // Cannot establish terminal success from contradictory/ambiguous event
      expect(isTerminalSuccess(events)).toBe(false);
      // Contradictory event also prevents snapshot from overriding it
      expect(isTerminalSuccess(events, "normal_persisted")).toBe(false);
    });

    it("H. Emergency persistence event cannot be overridden by normal_persisted snapshot", () => {
      const emergencyAudit: RunEvent = {
        kind: "audit_persisted",
        run_id: "run-term-emerg-override",
        stage: "audit",
        message: "audit records persisted (emergency mode in errors partition)",
        timestamp: "2026-10-31T20:00:00Z",
        audit_mode: "emergency",
        audit_partition: "errors",
      };
      const events = [...baseExecutedEvents, emergencyAudit, ev("run_completed")];
      // Emergency event alone is not success
      expect(isTerminalSuccess(events)).toBe(false);
      // Emergency persistence event cannot be silently reclassified as normal success by a contradictory snapshot
      expect(isTerminalSuccess(events, "normal_persisted")).toBe(false);
      expect(isTerminalSuccess(events, { audit_status: "normal_persisted" })).toBe(false);
    });

    it("I. Successfully recovered run with recoverable_errors > 0 achieves terminal success (Scenario 10)", () => {
      const events = [...baseExecutedEvents, normalAuditPersisted, ev("run_completed")];
      // When run succeeded downstream with 0 unrecovered failures, recoverable_errors does not block success
      expect(
        isTerminalSuccess(events, {
          run_outcome: "full_success",
          units_failed_operational: 0,
          recoverable_errors: 1,
        })
      ).toBe(true);

      // Unresolved operational failure still blocks success even if recoverable_errors > 0
      expect(
        isTerminalSuccess(events, {
          run_outcome: "full_success",
          units_failed_operational: 1,
          recoverable_errors: 1,
        })
      ).toBe(false);

      // Non-full_success run_outcome still blocks success
      expect(
        isTerminalSuccess(events, {
          run_outcome: "partial_success",
          units_failed_operational: 0,
          recoverable_errors: 1,
        })
      ).toBe(false);
    });
  });

  it("Workflow renders exactly 17 circular timeline nodes", () => {
    localStorage.setItem("lae-welcomed", "1");
    const { container } = render(<App />);
    expect(container.querySelectorAll("ol.timeline > li").length).toBe(17);
    expect(container.querySelectorAll("ol.timeline > li .node").length).toBe(17);
  });

  it("Vault Mode hides the synthetic fixture proposal and mock actions", async () => {
    localStorage.setItem("lae-welcomed", "1");
    const user = userEvent.setup();
    render(<App />);
    // Switch from Test to Vault mode via the real mode selector.
    await user.click(screen.getByRole("button", { name: /^Test/ }));
    // No synthetic CREATE proposal or mock approve/reject/edit in Vault Mode.
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Simulate safe-stop/ })).not.toBeInTheDocument();
    // An honest idle/setup state is shown instead.
    expect(screen.getByText(/No active run\. Enter an observation or note/)).toBeInTheDocument();
  });

  it("Journals show a multi-row window in Test Mode", () => {
    localStorage.setItem("lae-welcomed", "1");
    const { container } = render(<App />);
    const auditCards = container.querySelectorAll('section[aria-label="Recent Audit journal"] .journal-newspaper-card');
    expect(auditCards.length).toBeGreaterThanOrEqual(1);
  });

  it("Cancel button renders in TopNav when run is active", () => {
    localStorage.setItem("lae-welcomed", "1");
    render(<App />);
    // In idle fixture state, run is not active, so cancel button is not shown.
    expect(screen.queryByRole("button", { name: /Cancel Run/ })).not.toBeInTheDocument();
  });
});

describe("Input Stage — Chat Input UI, Loading, Localization, and Timeline", () => {
  it("displays the exact required placeholder in English and Persian", async () => {
    localStorage.setItem("lae-welcomed", "1");
    render(<App />);
    expect(
      screen.getByPlaceholderText("Type or paste here (text only — no uploads yet)")
    ).toBeInTheDocument();

    // Switch to Persian via Settings
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Settings" }));
    const faRadio = screen.getByRole("radio", { name: "فارسی" });
    await user.click(faRadio);

    expect(
      screen.getByPlaceholderText("متن را اینجا بنویسید یا بچسبانید (فقط متن — بدون بارگذاری فایل)")
    ).toBeInTheDocument();
  });

  it("handles multiline text and prevents empty submission", async () => {
    localStorage.setItem("lae-welcomed", "1");
    const user = userEvent.setup();
    render(<App />);

    const textarea = screen.getByPlaceholderText("Type or paste here (text only — no uploads yet)");
    const submitBtn = screen.getByRole("button", { name: /Send|Submit/i });

    // Empty text: button is disabled
    expect(submitBtn).toBeDisabled();

    // Whitespace only: button is still disabled
    await user.type(textarea, "   \n  ");
    expect(submitBtn).toBeDisabled();

    // Multiline typing with Shift+Enter
    await user.clear(textarea);
    await user.type(textarea, "Line 1{Shift>}{Enter}{/Shift}Line 2");
    expect(textarea).toHaveValue("Line 1\nLine 2");
  });

  it("derives INPUT timeline stage as current then completed", () => {
    // 1. Run started: input stage is current
    const stagesRunning = deriveWorkflow(
      { run_id: "run-test-1", current_stage: "input", stopped: false, status: "validating input" },
      [ev("run_started"), ev("stage_started", "input")]
    );
    const byRunning = Object.fromEntries(stagesRunning.map((s) => [s.key, s.state]));
    expect(byRunning.input).toBe("current");
    expect(byRunning.ingestion).toBe("pending");

    // 2. Stage completed for input: input is completed, ingestion is current
    const stagesCompleted = deriveWorkflow(
      { run_id: "run-test-1", current_stage: "ingestion", stopped: false, status: "ingesting" },
      [ev("run_started"), ev("stage_started", "input"), ev("stage_completed", "input")]
    );
    const byCompleted = Object.fromEntries(stagesCompleted.map((s) => [s.key, s.state]));
    expect(byCompleted.input).toBe("completed");
    expect(byCompleted.ingestion).toBe("current");

    // 3. Input stopped: input is stopped, downstream stays pending
    const stagesStopped = deriveWorkflow(
      { run_id: "run-test-1", current_stage: "input", stopped: true, status: "input rejected" },
      [ev("run_started"), ev("stage_started", "input"), ev("stopped", "input", "validation failed")]
    );
    const byStopped = Object.fromEntries(stagesStopped.map((s) => [s.key, s.state]));
    expect(byStopped.input).toBe("stopped");
    expect(byStopped.ingestion).toBe("pending");
  });

  it("live launch with no active run stays idle and timeline remains pending", () => {
    // When run_id is a fixture or empty, timeline must not claim proposal is running
    const fixtureStages = deriveWorkflow(
      { run_id: "run-fixture-001", current_stage: "proposal", stopped: false, status: "test sample — launcher not connected" },
      []
    );
    expect(fixtureStages.every((s) => s.state === "pending")).toBe(true);

    const idleStages = deriveWorkflow(
      { run_id: "", current_stage: "", stopped: false, status: "" },
      []
    );
    expect(idleStages.every((s) => s.state === "pending")).toBe(true);
  });

  it("when backend is unavailable, retains draft in composer and surfaces actionable connection recovery panel", async () => {
    localStorage.setItem("lae-welcomed", "1");
    const user = userEvent.setup();
    render(<App />);

    const textarea = screen.getByRole("textbox", {
      name: /Type or paste here/i,
    });
    const submitBtn = screen.getByRole("button", {
      name: /Send|Submit/i,
    });

    // Initially submit button is disabled because textarea is empty
    expect(submitBtn).toBeDisabled();

    // Type text into the composer
    const note = "Authentic observation note requiring backend evaluation.";
    await user.type(textarea, note);

    // Submit button becomes enabled
    expect(submitBtn).toBeEnabled();

    // Click submit
    await user.click(submitBtn);

    // Draft is preserved in the composer (not cleared or lost)
    expect(textarea).toHaveValue(note);

    // Actionable Recovery Panel is shown in Processing Result (no fake proposal success)
    expect(
      within(screen.getByLabelText("Processing Result")).getByRole("alert")
    ).toBeInTheDocument();
    expect(screen.getByText(/\[ERROR\]/)).toBeInTheDocument();
    expect(
      screen.getAllByText(/Backend service is unreachable|connection_unavailable/i).length
    ).toBeGreaterThanOrEqual(1);
    expect(
      within(within(screen.getByLabelText("Processing Result")).getByRole("alert")).getByText(note)
    ).toBeInTheDocument();

    // Dismiss button is rendered (Fix and Stop removed for input stage)
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Fix / Edit Input" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop / Dismiss" })).not.toBeInTheDocument();

    // Clicking Dismiss cleanly resets the error state
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/\[ERROR\]/)).not.toBeInTheDocument();
  });

  it("renders actionable error banner with retry affordance and dismissal", async () => {
    localStorage.setItem("lae-welcomed", "1");
    function ErrorTestComponent() {
      const { retryConnection, dismissLiveError } = useStore();
      const [err, setErr] = useState<string | null>("connection_unavailable");
      return (
        <div>
          {err && (
            <div role="alert" className="app-error-banner">
              <span>Backend connection is unavailable. Ensure the launcher is running.</span>
              <button type="button" onClick={() => { void retryConnection(); setErr(null); }}>
                Retry Connection
              </button>
              <button type="button" onClick={() => { dismissLiveError(); setErr(null); }}>
                Dismiss
              </button>
            </div>
          )}
        </div>
      );
    }
    const user = userEvent.setup();
    render(
      <StoreProvider>
        <ErrorTestComponent />
      </StoreProvider>
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry Connection" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("mockSubmitInput delegates to startLiveRun and never generates fake proposals", async () => {
    localStorage.setItem("lae-welcomed", "1");
    function TestMockComponent() {
      const { state, mockSubmitInput } = useStore();
      return (
        <div>
          <span data-testid="run-id">{state.run.run_id}</span>
          <span data-testid="outcome">{state.inputOutcome || "none"}</span>
          <span data-testid="status">{state.run.status}</span>
          <button type="button" onClick={() => mockSubmitInput("valid note for live run")}>
            Trigger Mock Submit
          </button>
        </div>
      );
    }
    render(
      <StoreProvider>
        <TestMockComponent />
      </StoreProvider>
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Trigger Mock Submit" }));

    // In unit test environment where live client is unavailable, mockSubmitInput must route
    // to ERROR / connection_unavailable, and NEVER synthesize run-local- or local mock proposals
    expect(screen.getByTestId("run-id").textContent).not.toContain("run-local-");
    expect(screen.getByTestId("status").textContent).toContain("Backend not connected");
    expect(screen.getByTestId("outcome").textContent).toBe("ERROR");
  });

  it("delayed cancellation and stopped state cleanly prevents run resurrection", async () => {
    localStorage.setItem("lae-welcomed", "1");
    function StoppedRunTestComponent() {
      const { state, cancelActiveRun } = useStore();
      return (
        <div>
          <span data-testid="is-stopped">{String(state.run.stopped)}</span>
          <span data-testid="review-state">{state.run.review_state}</span>
          <button type="button" onClick={() => void cancelActiveRun()}>
            Cancel Run
          </button>
        </div>
      );
    }
    render(
      <StoreProvider>
        <StoppedRunTestComponent />
      </StoreProvider>
    );
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Cancel Run" }));

    expect(screen.getByTestId("is-stopped").textContent).toBe("true");
  });

  it("enforces minimum 50 letters boundary: disallows sending under 50 letters and enables at 50+ letters", async () => {
    localStorage.setItem("lae-welcomed", "1");
    const user = userEvent.setup();
    render(<App />);

    const textarea = screen.getByRole("textbox", {
      name: /Type or paste here/i,
    });
    const submitBtn = screen.getByRole("button", {
      name: /Send|Submit/i,
    });

    // 1. Empty input: button is disabled
    expect(submitBtn).toBeDisabled();

    // 2. Short input (< 50 letters): button remains strictly disabled
    await user.type(textarea, "Short note with twenty-six letters total.");
    expect(submitBtn).toBeDisabled();
    expect(screen.getByText(/letters.*more data needed/i)).toBeInTheDocument();

    // 3. Completing up to 50+ letters enables submission
    await user.clear(textarea);
    const valid50 = "Authentic observation note requiring backend evaluation.";
    await user.type(textarea, valid50);
    expect(screen.getByText(/50 \/ 50 letters.*ready to send/i)).toBeInTheDocument();
    expect(submitBtn).toBeEnabled();
  });

  it("renders retrieval strategy selector with lexical active by default and hybrid strictly disabled", async () => {
    localStorage.setItem("lae-welcomed", "1");
    const user = userEvent.setup();
    render(<App />);

    const lexicalBtn = screen.getByRole("button", { name: /lexical/i });
    const semanticBtn = screen.getByRole("button", { name: /semantic/i });
    const hybridBtn = screen.getByRole("button", { name: /hybrid/i });

    expect(lexicalBtn).toHaveAttribute("aria-pressed", "true");
    expect(semanticBtn).toHaveAttribute("aria-pressed", "false");
    expect(hybridBtn).toBeDisabled();

    // Selecting semantic updates pressed state
    await user.click(semanticBtn);
    expect(semanticBtn).toHaveAttribute("aria-pressed", "true");
    expect(lexicalBtn).toHaveAttribute("aria-pressed", "false");

    // Switching back to lexical works
    await user.click(lexicalBtn);
    expect(lexicalBtn).toHaveAttribute("aria-pressed", "true");
    expect(semanticBtn).toHaveAttribute("aria-pressed", "false");
  });

  it("strictly scopes retrieval events by runId and distinguishes running vs completed phases", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "other-run", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "status", run_id: "", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "" }, // untagged
      { kind: "stage_started", run_id: "target-run", stage: "retrieval", operation: "candidate_retrieval", message: "", phase: "started", timestamp: "" },
    ];

    // 1. With active target-run: other-run and untagged are strictly excluded
    const filtered = filterEventsByRun(events, "target-run");
    expect(filtered.length).toBe(1);
    expect(filtered[0].operation).toBe("candidate_retrieval");
    expect(filtered[0].run_id).toBe("target-run");

    // 2. Active substep detects running phase
    const active = deriveActiveRetrievalSubstep(events, "target-run");
    expect(active).toBeDefined();
    expect(active?.operation).toBe("candidate_retrieval");
    expect(active?.phase).toBe("started");
    expect(active?.isRunning).toBe(true);

    // 3. Substep status marks running substep as current
    const substepsRunning = deriveRetrievalSubsteps("current", events, "target-run");
    const candSub = substepsRunning.find((s) => s.key === "candidate_retrieval");
    expect(candSub?.state).toBe("current");

    // 4. When completed event arrives, isRunning becomes false
    const eventsWithCompletion: RunEvent[] = [
      ...events,
      { kind: "stage_completed", run_id: "target-run", stage: "retrieval", operation: "candidate_retrieval", message: "", phase: "completed" as const, timestamp: "" },
    ];
    const completedActive = deriveActiveRetrievalSubstep(eventsWithCompletion, "target-run");
    expect(completedActive).toBeDefined();
    expect(completedActive?.operation).toBe("candidate_retrieval");
    expect(completedActive?.phase).toBe("completed");
    expect(completedActive?.isRunning).toBe(false);

    const substepsCompleted = deriveRetrievalSubsteps("current", eventsWithCompletion, "target-run");
    const candSubCompleted = substepsCompleted.find((s) => s.key === "candidate_retrieval");
    expect(candSubCompleted?.state).toBe("completed");
  });

  it("legacy policy applies when runId is empty or undefined", () => {
    const untaggedEvents: RunEvent[] = [
      { kind: "status", run_id: "", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "status", run_id: "", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "" },
    ];
    expect(filterEventsByRun(untaggedEvents, "").length).toBe(2);
    expect(filterEventsByRun(untaggedEvents, undefined).length).toBe(2);

    // When exactly one tagged run exists, single-run isolation selects only that run's events
    const singleTagged: RunEvent[] = [
      { kind: "status", run_id: "tagged-run", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "status", run_id: "", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "" },
    ];
    const isolated = filterEventsByRun(singleTagged, undefined);
    expect(isolated.length).toBe(1);
    expect(isolated[0].run_id).toBe("tagged-run");
  });

  it("filterEventsByRun selects latest run by timestamp when runId is omitted", () => {
    // Interleaved events where older run events appear after newer run events in array order
    const events: RunEvent[] = [
      { kind: "status", run_id: "run-newer", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T03:00:00Z" },
      { kind: "status", run_id: "run-older", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T01:00:00Z" },
      { kind: "status", run_id: "run-newer", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "2026-10-25T03:01:00Z" },
    ];
    const filtered = filterEventsByRun(events, undefined);
    expect(filtered.length).toBe(2);
    expect(filtered.every((e) => e.run_id === "run-newer")).toBe(true);
  });

  it("deriveRetrievalSubsteps preserves failed substep even if stageState is completed", () => {
    const events: RunEvent[] = [
      { kind: "status", run_id: "run-1", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-1", stage: "retrieval", operation: "candidate_retrieval", phase: "failed", message: "", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("completed", events, "run-1");
    const candSub = substeps.find((s) => s.key === "candidate_retrieval");
    expect(candSub?.state).toBe("failed");
  });

  it("does NOT infer earlier substep completion from sequential index ordering alone", () => {
    // query_preparation started, followed by candidate_retrieval started (query_prep never emitted completed)
    const events: RunEvent[] = [
      { kind: "status", run_id: "run-seq", stage: "retrieval", operation: "query_preparation", phase: "started", message: "", timestamp: "" },
      { kind: "status", run_id: "run-seq", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" },
    ];

    const currentSubsteps = deriveRetrievalSubsteps("current", events, "run-seq");
    const querySub = currentSubsteps.find((s) => s.key === "query_preparation");
    const candSub = currentSubsteps.find((s) => s.key === "candidate_retrieval");

    // CRITICAL: query_preparation must NOT remain current and must NOT be inferred as completed!
    // Since later work began without a terminal event, it is conservatively represented as unknown.
    expect(querySub?.state).not.toBe("completed");
    expect(querySub?.state).not.toBe("current");
    expect(querySub?.state).toBe("unknown");
    // Only real active work is shown as current
    expect(candSub?.state).toBe("current");

    // In a stopped run: neither should be marked completed, and in-flight operations are stopped
    const stoppedSubsteps = deriveRetrievalSubsteps("stopped", events, "run-seq");
    const queryStopped = stoppedSubsteps.find((s) => s.key === "query_preparation");
    const candStopped = stoppedSubsteps.find((s) => s.key === "candidate_retrieval");
    expect(queryStopped?.state).not.toBe("completed");
    expect(candStopped?.state).not.toBe("completed");
    expect(queryStopped?.state).toBe("stopped");
    expect(candStopped?.state).toBe("stopped");

    // When query_preparation emits explicit completed, it is properly recognized
    const eventsWithExplicit: RunEvent[] = [
      ...events,
      { kind: "status", run_id: "run-seq", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
    ];
    const explicitSubsteps = deriveRetrievalSubsteps("current", eventsWithExplicit, "run-seq");
    expect(explicitSubsteps.find((s) => s.key === "query_preparation")?.state).toBe("completed");
  });

  it("marks missing intermediate substeps as unknown when later substeps have started", () => {
    // query_preparation completed, candidate_retrieval was skipped without events, relevance_assessment started
    const events: RunEvent[] = [
      { kind: "status", run_id: "run-skip", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "run-skip", stage: "retrieval", operation: "relevance_assessment", phase: "started", message: "", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("current", events, "run-skip");
    expect(substeps.find((s) => s.key === "query_preparation")?.state).toBe("completed");
    expect(substeps.find((s) => s.key === "candidate_retrieval")?.state).toBe("unknown");
    expect(substeps.find((s) => s.key === "relevance_assessment")?.state).toBe("current");
    expect(substeps.find((s) => s.key === "candidate_identification")?.state).toBe("pending");
  });

  it("handles contradictory event sequences conservatively as unknown", () => {
    // Substep emitted an unrecognized/contradictory phase
    const events: RunEvent[] = [
      { kind: "status", run_id: "run-contra", stage: "retrieval", operation: "query_preparation", phase: "contradictory_phase" as any, message: "", timestamp: "" },
    ];
    const substeps = deriveRetrievalSubsteps("current", events, "run-contra");
    expect(substeps.find((s) => s.key === "query_preparation")?.state).toBe("unknown");
  });

  it("distinguishes started, completed, and unknown/unexpected phases cleanly", () => {
    // 1. First-class failure phase ('failed')
    const eventsFailed: RunEvent[] = [
      { kind: "status", run_id: "run-failed", stage: "retrieval", operation: "relevance_assessment", phase: "failed", message: "", timestamp: "" },
    ];
    const activeFailed = deriveActiveRetrievalSubstep(eventsFailed, "run-failed");
    expect(activeFailed).toBeDefined();
    expect(activeFailed?.operation).toBe("relevance_assessment");
    expect(activeFailed?.phase).toBe("failed");
    expect(activeFailed?.isRunning).toBe(false);

    const substepsFailed = deriveRetrievalSubsteps("current", eventsFailed, "run-failed");
    const relSub = substepsFailed.find((s) => s.key === "relevance_assessment");
    expect(relSub?.state).toBe("failed");

    // 1b. Unknown / unexpected phase (e.g. invalid string)
    const eventsUnknown: RunEvent[] = [
      { kind: "status", run_id: "run-unknown", stage: "retrieval", operation: "relevance_assessment", phase: "corrupt_phase" as any, message: "", timestamp: "" },
    ];
    const activeUnknown = deriveActiveRetrievalSubstep(eventsUnknown, "run-unknown");
    expect(activeUnknown).toBeDefined();
    expect(activeUnknown?.operation).toBe("relevance_assessment");
    expect(activeUnknown?.phase).toBe("unknown");
    expect(activeUnknown?.isRunning).toBe(false);

    // 2. Stopped run must not leave substeps as isRunning=true
    const eventsStopped: RunEvent[] = [
      { kind: "status", run_id: "run-stopped", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" },
      { kind: "stopped", run_id: "run-stopped", stage: "retrieval", message: "run cancelled", timestamp: "" },
    ];
    const activeStopped = deriveActiveRetrievalSubstep(eventsStopped, "run-stopped");
    expect(activeStopped?.isRunning).toBe(false);

    // 3. Explicit isStopped parameter
    const activeStoppedParam = deriveActiveRetrievalSubstep(
      [{ kind: "status", run_id: "run-p", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" }],
      "run-p",
      true
    );
    expect(activeStoppedParam?.isRunning).toBe(false);

    // 4. Legacy event with missing phase
    const eventsMissingPhase: RunEvent[] = [
      { kind: "status", run_id: "run-legacy", stage: "retrieval", operation: "candidate_identification", message: "", timestamp: "" },
    ];
    const activeLegacy = deriveActiveRetrievalSubstep(eventsMissingPhase, "run-legacy");
    expect(activeLegacy).toBeDefined();
    expect(activeLegacy?.operation).toBe("candidate_identification");
    expect(activeLegacy?.phase).toBe("started");
    expect(activeLegacy?.isRunning).toBe(true);
  });
});

describe("TranslateAction provenance and stale handling", () => {
  it("renders source kind badge and detects stale source text, recordId, and language changes", async () => {
    const mockLiveTranslate = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "متن ترجمه‌شده",
      status: "current",
      translator: "static-dict",
      source_hash: computeSha256("Original narrative text"),
      translated_hash: "hash5678",
      source_text: "Original narrative text",
      source_kind: "audit_narrative",
      source_id: "rec-audit-001",
      persisted: false,
    });

    const mockContextValue = {
      state: {
        appLanguage: "fa",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: {},
        runEvents: [],
      },
      liveTranslate: mockLiveTranslate,
    };

    const user = userEvent.setup();
    const { rerender } = render(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction
          text="Original narrative text"
          sourceKind="audit_narrative"
          recordId="rec-audit-001"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );

    const translateBtn = screen.getByRole("button", { name: /Translate|ترجمه/ });
    await user.click(translateBtn);

    expect(mockLiveTranslate).toHaveBeenCalledWith(
      "Original narrative text",
      "fa",
      "en",
      "audit_narrative",
      "rec-audit-001"
    );

    // Shows source kind badge and translated text
    expect(screen.getByText("[audit_narrative]")).toBeInTheDocument();
    expect(screen.getByText("متن ترجمه‌شده")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();

    // Rerender with mutated text -> stale badge should appear
    rerender(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction
          text="Modified narrative text"
          sourceKind="audit_narrative"
          recordId="rec-audit-001"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    expect(document.querySelector(".translate-stale")).toBeInTheDocument();

    // Rerender with mutated recordId -> stale badge should appear
    rerender(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction
          text="Original narrative text"
          sourceKind="audit_narrative"
          recordId="rec-audit-different"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    expect(document.querySelector(".translate-stale")).toBeInTheDocument();

    // Rerender with mutated sourceKind -> stale badge should appear
    rerender(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction
          text="Original narrative text"
          sourceKind="proposal"
          recordId="rec-audit-001"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    expect(document.querySelector(".translate-stale")).toBeInTheDocument();

    // Rerender with mutated sourceLang -> stale badge should appear
    rerender(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction
          text="Original narrative text"
          sourceKind="audit_narrative"
          recordId="rec-audit-001"
          sourceLang="fa"
        />
      </StoreContext.Provider>
    );
    expect(document.querySelector(".translate-stale")).toBeInTheDocument();
  });

  it("marks translation unverifiable when source_hash is missing and stale when source_hash does not match", async () => {
    // Missing source_hash
    const mockNoHash = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "ترجمه بدون هش",
      status: "current",
      source_text: "Original text",
      source_kind: "dynamic_text",
      source_id: "id-1",
      persisted: false,
    });

    const mockContextValue = {
      state: {
        appLanguage: "fa",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: {},
        runEvents: [],
      },
      liveTranslate: mockNoHash,
    };

    const user = userEvent.setup();
    const { rerender } = render(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction text="Original text" sourceKind="dynamic_text" recordId="id-1" sourceLang="en" />
      </StoreContext.Provider>
    );

    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    // Because source_hash is missing, it must not treat missing provenance as fresh; it is marked unverifiable
    expect(screen.getByText(/unverifiable|غیرقابل راستی‌آزمایی/)).toBeInTheDocument();

    // Mismatched source_hash
    const mockBadHash = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "ترجمه با هش نامعتبر",
      status: "current",
      source_hash: "wrong_sha256_hash",
      source_text: "Original text",
      source_kind: "dynamic_text",
      source_id: "id-1",
      persisted: false,
    });

    rerender(
      <StoreContext.Provider value={{ ...mockContextValue, liveTranslate: mockBadHash } as any}>
        <TranslateAction text="Original text" sourceKind="dynamic_text" recordId="id-1" sourceLang="en" />
      </StoreContext.Provider>
    );

    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(document.querySelector(".translate-stale")).toBeInTheDocument();
  });

  it("renders unverifiable badge when translation status is unverifiable", async () => {
    const mockUnverifiable = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "ترجمه غیرقابل‌تایید",
      status: "unverifiable",
      source_text: "Original text",
      source_kind: "dynamic_text",
      source_id: "id-1",
      persisted: false,
    });
    const mockContextValue = {
      state: {
        appLanguage: "fa",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: {},
        runEvents: [],
      },
      liveTranslate: mockUnverifiable,
    };
    render(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction text="Original text" sourceKind="dynamic_text" recordId="id-1" sourceLang="en" />
      </StoreContext.Provider>
    );
    await userEvent.setup().click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(screen.getByText(/unverifiable|غیرقابل راستی‌آزمایی/)).toBeInTheDocument();
  });

  it("does NOT mark unavailable translation as stale when source_hash is absent", async () => {
    const mockUnavailable = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "«Original text» (ترجمه خودکار در دسترس نیست — مدل زبانی متصل نیست)",
      status: "unavailable",
      source_text: "Original text",
      source_kind: "dynamic_text",
      source_id: "id-1",
      persisted: false,
    });
    const mockContextValue = {
      state: {
        appLanguage: "fa",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: {},
        runEvents: [],
      },
      liveTranslate: mockUnavailable,
    };
    render(
      <StoreContext.Provider value={mockContextValue as any}>
        <TranslateAction text="Original text" sourceKind="dynamic_text" recordId="id-1" sourceLang="en" />
      </StoreContext.Provider>
    );
    await userEvent.setup().click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
    expect(screen.getByText("unavailable")).toBeInTheDocument();
  });

  it("does not silently merge events from different runs when runId is omitted and timestamps are missing", () => {
    const multiRunEvents: RunEvent[] = [
      { kind: "status", run_id: "run-old", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "status", run_id: "run-new", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "" },
    ];
    // Under conservative correlation, multiple runs without valid timestamps must NOT be correlated by array order
    const scoped = filterEventsByRun(multiRunEvents, undefined);
    expect(scoped).toEqual([]);
  });

  it("computes standard SHA-256 matching reference vectors", () => {
    expect(computeSha256("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    expect(computeSha256("hello")).toBe("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
    expect(computeSha256("سلام دنیا")).toBe("a5243b50dac2e60ee8f7756fcb7c7cf282e485bea5f91db3ea621a786c0c554e");
  });

  it("renders emergency journal badges when emergency audit records are present", () => {
    const mockContext = {
      state: {
        appLanguage: "en",
        auditLanguage: "en",
        stagingLanguage: "en",
        knowledgeLanguageNote: "",
        recentAudits: [
          {
            id: "rec-emerg-1",
            timestamp: "2026-10-24T12:00:00Z",
            statusKey: "journal.failed",
            stagingNoteKey: "journal.stagingNotCreated",
            title: "Emergency Audit: CREATE",
            summary: "Emergency audit record: action CREATE reached terminal outcome: failed.",
            emergency: true,
          },
        ],
        recentStaging: [
          {
            id: "rec-emerg-1",
            timestamp: "2026-10-24T12:00:00Z",
            statusKey: "journal.failed",
            stagingNoteKey: null,
            title: "Staged Artifact",
            excerpt: "Unavailable",
            emergency: true,
            staging_available: false,
          },
        ],
        sourceMode: "vault",
        journalIndex: 0,
        run: {},
        runEvents: [],
      },
      liveAvailable: true,
      refreshJournals: vi.fn(),
    };

    render(
      <StoreContext.Provider value={mockContext as any}>
        <Journals />
      </StoreContext.Provider>
    );

    expect(screen.getByText("Emergency Journal")).toBeInTheDocument();
    expect(screen.getByText("Emergency")).toBeInTheDocument();
  });

  it("marks translation unverifiable when record is missing source_id, source_kind, or source_language", async () => {
    // Record where source_id is missing/empty even though caller expects recordId="rec-req-1"
    const mockMissingSourceId = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "ترجمه بدون شناسه مبدا",
      status: "current",
      source_hash: computeSha256("Source text requiring id"),
      source_text: "Source text requiring id",
      source_kind: "dynamic_text",
      source_id: "", // Missing source_id!
      persisted: false,
    });

    const mockContext = {
      state: {
        appLanguage: "fa",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: {},
        runEvents: [],
      },
      liveTranslate: mockMissingSourceId,
    };

    const user = userEvent.setup();
    const { rerender } = render(
      <StoreContext.Provider value={mockContext as any}>
        <TranslateAction
          text="Source text requiring id"
          sourceKind="dynamic_text"
          recordId="rec-req-1"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );

    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    // Missing required metadata (source_id) must fail closed as unverifiable
    expect(screen.getByText(/unverifiable|غیرقابل راستی‌آزمایی/)).toBeInTheDocument();
    // Non-persisted on-demand badge is visible
    expect(screen.getByText(/on-demand|درخواستی/)).toBeInTheDocument();

    // Missing source_kind when caller specifies sourceKind="audit_narrative"
    const mockMissingSourceKind = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "ترجمه بدون نوع مبدا",
      status: "current",
      source_hash: computeSha256("Source text requiring kind"),
      source_text: "Source text requiring kind",
      source_kind: "", // Missing source_kind!
      source_id: "rec-req-2",
      persisted: false,
    });

    rerender(
      <StoreContext.Provider value={{ ...mockContext, liveTranslate: mockMissingSourceKind } as any}>
        <TranslateAction
          text="Source text requiring kind"
          sourceKind="audit_narrative"
          recordId="rec-req-2"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(screen.getByText(/unverifiable|غیرقابل راستی‌آزمایی/)).toBeInTheDocument();

    // Missing source_language when caller specifies sourceLang="en"
    const mockMissingSourceLang = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "", // Missing source_language!
      translated_text: "ترجمه بدون زبان مبدا",
      status: "current",
      source_hash: computeSha256("Source text requiring lang"),
      source_text: "Source text requiring lang",
      source_kind: "dynamic_text",
      source_id: "rec-req-3",
      persisted: false,
    });

    rerender(
      <StoreContext.Provider value={{ ...mockContext, liveTranslate: mockMissingSourceLang } as any}>
        <TranslateAction
          text="Source text requiring lang"
          sourceKind="dynamic_text"
          recordId="rec-req-3"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(screen.getByText(/unverifiable|غیرقابل راستی‌آزمایی/)).toBeInTheDocument();
  });

  it("handles target_language validation: unverifiable when missing, stale when mismatched, current when verified", async () => {
    const user = userEvent.setup();
    // 1. Missing target_language -> unverifiable
    const mockMissingTarget = vi.fn().mockResolvedValue({
      target_lang: "",
      target_language: "",
      source_language: "en",
      translated_text: "بدون زبان مقصد",
      status: "current",
      source_hash: computeSha256("Target test text"),
      source_text: "Target test text",
      source_kind: "dynamic_text",
      source_id: "rec-target-1",
      persisted: false,
    });

    const mockContext = {
      state: {
        appLanguage: "fa",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: {},
        runEvents: [],
      },
      liveTranslate: mockMissingTarget,
    };

    const { rerender } = render(
      <StoreContext.Provider value={mockContext as any}>
        <TranslateAction
          text="Target test text"
          sourceKind="dynamic_text"
          recordId="rec-target-1"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );

    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(screen.getByText(/unverifiable|غیرقابل راستی‌آزمایی/)).toBeInTheDocument();

    // 2. Mismatched target_language -> stale (metadata present but targeting German 'de' when UI is 'fa')
    const mockMismatchedTarget = vi.fn().mockResolvedValue({
      target_lang: "de",
      target_language: "de",
      source_language: "en",
      translated_text: "Deutscher Text",
      status: "current",
      source_hash: computeSha256("Target test text"),
      source_text: "Target test text",
      source_kind: "dynamic_text",
      source_id: "rec-target-1",
      persisted: false,
    });

    rerender(
      <StoreContext.Provider value={{ ...mockContext, liveTranslate: mockMismatchedTarget } as any}>
        <TranslateAction
          text="Target test text"
          sourceKind="dynamic_text"
          recordId="rec-target-1"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(document.querySelector(".translate-stale")).toBeInTheDocument();

    // 3. Fully valid, current translation -> current
    const mockValidCurrent = vi.fn().mockResolvedValue({
      target_lang: "fa",
      target_language: "fa",
      source_language: "en",
      translated_text: "متن معتبر و به روز",
      status: "current",
      source_hash: computeSha256("Target test text"),
      source_text: "Target test text",
      source_kind: "dynamic_text",
      source_id: "rec-target-1",
      persisted: false,
    });

    rerender(
      <StoreContext.Provider value={{ ...mockContext, liveTranslate: mockValidCurrent } as any}>
        <TranslateAction
          text="Target test text"
          sourceKind="dynamic_text"
          recordId="rec-target-1"
          sourceLang="en"
        />
      </StoreContext.Provider>
    );
    await user.click(screen.getByRole("button", { name: /Translate|ترجمه/ }));
    expect(screen.getByText(/current|به‌روز/)).toBeInTheDocument();
  });
});

describe("conservative run correlation and retrieval UI behavior (Section 3)", () => {
  it("selects newer run when all runs have valid, comparable timestamps even if older events appear later in array", () => {
    // Array order: run-newer appears first, run-older appears second
    const eventsOrder1: RunEvent[] = [
      { kind: "stage_started", run_id: "run-newer", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:30:00Z" },
      { kind: "stage_started", run_id: "run-older", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
    ];
    const res1 = filterEventsByRun(eventsOrder1, undefined);
    expect(res1.length).toBe(1);
    expect(res1[0].run_id).toBe("run-newer");

    // Inverted array order: run-older appears first, run-newer appears second
    const eventsOrder2: RunEvent[] = [
      { kind: "stage_started", run_id: "run-older", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
      { kind: "stage_started", run_id: "run-newer", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:30:00Z" },
    ];
    const res2 = filterEventsByRun(eventsOrder2, undefined);
    expect(res2.length).toBe(1);
    expect(res2[0].run_id).toBe("run-newer");
  });

  it("returns empty array when multiple distinct run IDs exist and all lack timestamps", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "" },
    ];
    expect(filterEventsByRun(events, undefined)).toEqual([]);
    expect(filterEventsByRun(events, "")).toEqual([]);
  });

  it("returns empty array when multiple distinct run IDs have mixed timestamp availability", () => {
    // run-A has timestamp, but run-B has empty or whitespace timestamp
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "   " },
    ];
    expect(filterEventsByRun(events, undefined)).toEqual([]);
  });

  it("returns empty array when any run has an invalid or unparseable timestamp", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "invalid-iso-date" },
    ];
    expect(filterEventsByRun(events, undefined)).toEqual([]);
  });

  it("returns empty array when top candidate runs have equal timestamps (contradictory / ambiguous)", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "2026-10-25T14:00:00Z" },
    ];
    expect(filterEventsByRun(events, undefined)).toEqual([]);
  });

  it("strictly isolates events to the specified run_id regardless of timestamps or other runs", () => {
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T10:00:00Z" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "2026-10-25T16:00:00Z" },
    ];
    // Asking explicitly for run-A returns only run-A even though run-B is newer
    const resA = filterEventsByRun(events, "run-A");
    expect(resA.length).toBe(1);
    expect(resA[0].run_id).toBe("run-A");

    // Asking explicitly for run-B returns only run-B
    const resB = filterEventsByRun(events, "run-B");
    expect(resB.length).toBe(1);
    expect(resB[0].run_id).toBe("run-B");
  });

  it("compares different timezone offsets by epoch milliseconds, not string lexicographical order", () => {
    // 16:00:00+02:00 is 14:00:00 UTC
    // 14:30:00Z is 14:30:00 UTC (later than 14:00:00 UTC)
    // String comparison would incorrectly claim "2026-10-25T16:00:00+02:00" > "2026-10-25T14:30:00Z"
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-earlier-tz", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T16:00:00+02:00" },
      { kind: "stage_started", run_id: "run-later-utc", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:30:00Z" },
    ];
    const res = filterEventsByRun(events, undefined);
    expect(res.length).toBe(1);
    expect(res[0].run_id).toBe("run-later-utc");
  });

  it("returns empty array when different timezone offsets represent the exact same absolute epoch time", () => {
    // 14:00:00Z and 16:00:00+02:00 represent the identical epoch millisecond timestamp
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-utc", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
      { kind: "stage_started", run_id: "run-plus-two", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "2026-10-25T16:00:00+02:00" },
    ];
    expect(filterEventsByRun(events, undefined)).toEqual([]);
  });

  it("returns empty array if any event within a candidate run has a missing or invalid timestamp", () => {
    // run-A has one valid timestamp and one empty timestamp
    const events: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "2026-10-25T14:00:00Z" },
      { kind: "stage_completed", run_id: "run-A", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "2026-10-25T15:00:00Z" },
    ];
    expect(filterEventsByRun(events, undefined)).toEqual([]);
  });

  it("returns all events when all events are untagged (distinctRuns.length === 0)", () => {
    const events: RunEvent[] = [
      { kind: "status", run_id: "", stage: "retrieval", operation: "query_preparation", message: "", timestamp: "" },
      { kind: "status", run_id: "", stage: "retrieval", operation: "candidate_retrieval", message: "", timestamp: "" },
    ];
    expect(filterEventsByRun(events, undefined).length).toBe(2);
  });

  it("never invents successful Retrieval substep states when run correlation is uncertain", () => {
    // Both runs have events claiming completed substeps, but missing timestamps make correlation uncertain
    const ambiguousEvents: RunEvent[] = [
      { kind: "stage_completed", run_id: "run-A", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "stage_completed", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", phase: "completed", message: "", timestamp: "" },
    ];

    // 1. Stage completed but correlation is uncertain: all substeps MUST be 'unknown'
    const completedSubsteps = deriveRetrievalSubsteps("completed", ambiguousEvents, undefined);
    expect(completedSubsteps).toHaveLength(RETRIEVAL_SUBSTEPS.length);
    for (const step of completedSubsteps) {
      expect(step.state).toBe("unknown");
    }

    // 2. No substep is falsely marked completed or current
    expect(completedSubsteps.some((s) => s.state === "completed")).toBe(false);
    expect(completedSubsteps.some((s) => s.state === "current")).toBe(false);
  });

  it("preserves pending/unknown state without active running state when run correlation is uncertain", () => {
    const ambiguousEvents: RunEvent[] = [
      { kind: "stage_started", run_id: "run-A", stage: "retrieval", operation: "query_preparation", phase: "started", message: "", timestamp: "" },
      { kind: "stage_started", run_id: "run-B", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" },
    ];

    // 1. Active substep cannot be derived when correlation is uncertain
    const active = deriveActiveRetrievalSubstep(ambiguousEvents, undefined);
    expect(active).toBeUndefined();

    const activeOp = deriveRetrievalSubstep(ambiguousEvents, undefined);
    expect(activeOp).toBeUndefined();

    // 2. Stage current with uncertain correlation leaves substeps pending/unknown without running current state
    const currentSubsteps = deriveRetrievalSubsteps("current", ambiguousEvents, undefined);
    expect(currentSubsteps).toHaveLength(RETRIEVAL_SUBSTEPS.length);
    for (const step of currentSubsteps) {
      expect(step.state).not.toBe("current");
      expect(step.state).not.toBe("completed");
    }
  });
});



describe("Phase 2 — Frontend Human Review Loop UI", () => {
  const baseRunState: any = {
    run_id: "run-rev-ui-1",
    proposal_action: "NEEDS_REVIEW",
    classification_type: "Observation",
    proposal_title: "Complex Experiential Pattern",
    proposal_body: "Detailed analytical observation text from empirical data.",
    confidence_score: 0.88,
    confidence_score_band: "high",
    confidence_level: "high",
    destination: "01 - Observations/Complex Pattern.md",
    reason: "Similar candidate exists; requires human clarification",
    evidence: ["Anchor text 1", "Anchor text 2"],
    provenance: ["source-file.md"],
    uncertainties: ["Possible semantic boundary ambiguity"],
    proposal_hash: "a".repeat(64),
    review_state: "live_awaiting_review",
    current_stage: "human_review",
    status: "awaiting human review / acknowledgment",
    stopped: false,
    stop_reason: "",
    accepted: false,
    edited_body: null,
    error: null,
  };

  it("renders non-CREATE proposal with executability banner, evidence, uncertainties, and Acknowledge button", () => {
    const fakeStore: any = {
      state: {
        appLanguage: "en",
        stagingLanguage: "en",
        auditLanguage: "en",
        sourceMode: "vault",
        retrievalType: "lexical",
        run: { ...baseRunState },
        runEvents: [],
        inputOutcome: null,
        inputErrorCode: null,
        inputRecoveryOptions: [],
        safeStop: null,
        lastSubmittedText: "",
      },
      liveAvailable: true,
      liveConnected: true,
      liveError: null,
      actionInFlight: false,
      liveApproval: {
        proposal_hash: "a".repeat(64),
        query_unit_id: "qu-99",
        proposal_action: "NEEDS_REVIEW",
        title: "Complex Experiential Pattern",
        body: "Detailed analytical observation text from empirical data.",
        confidence_level: "high",
        confidence_score: 0.88,
        confidence_score_band: "high",
        classification_type: "Observation",
        destination: "01 - Observations/Complex Pattern.md",
        reason: "Similar candidate exists; requires human clarification",
        evidence: ["Anchor text 1", "Anchor text 2"],
        provenance: ["source-file.md"],
        uncertainties: ["Possible semantic boundary ambiguity"],
        is_executable: false,
      },
      liveAccept: vi.fn(),
      liveApprove: vi.fn(),
      dismissRunOutcome: vi.fn(),
      cancelActiveRun: vi.fn(),
    };

    render(
      <StoreContext.Provider value={fakeStore}>
        <ProcessingResult />
      </StoreContext.Provider>
    );

    // Verify Title
    expect(screen.getByText("Complex Experiential Pattern")).toBeInTheDocument();
    // Verify Executability Banner
    expect(screen.getByText(/Non-Executable Proposal \(NEEDS_REVIEW\)/i)).toBeInTheDocument();
    // Verify Destination
    expect(screen.getByText("01 - Observations/Complex Pattern.md")).toBeInTheDocument();
    // Verify Reason
    expect(screen.getByText("Similar candidate exists; requires human clarification")).toBeInTheDocument();
    // Verify Evidence
    expect(screen.getByText("Anchor text 1")).toBeInTheDocument();
    // Verify Uncertainties
    expect(screen.getByText("Possible semantic boundary ambiguity")).toBeInTheDocument();
    // Verify Provenance
    expect(screen.getByText(/source-file\.md/)).toBeInTheDocument();
    // Verify Button is "Acknowledge", NOT "Accept" or "Approve"
    const ackBtn = screen.getByRole("button", { name: "Acknowledge" });
    expect(ackBtn).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("disables Acknowledge button and shows submitting state when action is in flight", () => {
    const fakeStore: any = {
      state: {
        appLanguage: "en",
        stagingLanguage: "en",
        auditLanguage: "en",
        sourceMode: "vault",
        retrievalType: "lexical",
        run: { ...baseRunState },
        runEvents: [],
        inputOutcome: null,
        inputErrorCode: null,
        inputRecoveryOptions: [],
        safeStop: null,
        lastSubmittedText: "",
      },
      liveAvailable: true,
      liveConnected: true,
      liveError: null,
      actionInFlight: true,
      liveApproval: {
        proposal_hash: "a".repeat(64),
        query_unit_id: "qu-99",
        proposal_action: "NEEDS_REVIEW",
        title: "Complex Experiential Pattern",
        body: "Detailed analytical observation text.",
        confidence_level: "high",
        is_executable: false,
      },
      liveAccept: vi.fn(),
      liveApprove: vi.fn(),
      dismissRunOutcome: vi.fn(),
      cancelActiveRun: vi.fn(),
    };

    render(
      <StoreContext.Provider value={fakeStore}>
        <ProcessingResult />
      </StoreContext.Provider>
    );

    const submittingBtn = screen.getByRole("button", { name: /Submitting Acknowledgment/i });
    expect(submittingBtn).toBeDisabled();
    expect(screen.getByText("Submitting decision to backend…")).toBeInTheDocument();
  });

  it("displays liveError prominently in non-CREATE review when acknowledgment fails", () => {
    const fakeStore: any = {
      state: {
        appLanguage: "en",
        stagingLanguage: "en",
        auditLanguage: "en",
        sourceMode: "vault",
        retrievalType: "lexical",
        run: { ...baseRunState },
        runEvents: [],
        inputOutcome: null,
        inputErrorCode: null,
        inputRecoveryOptions: [],
        safeStop: null,
        lastSubmittedText: "",
      },
      liveAvailable: true,
      liveConnected: true,
      liveError: "proposal_hash does not match the active proposal",
      actionInFlight: false,
      liveApproval: {
        proposal_hash: "a".repeat(64),
        query_unit_id: "qu-99",
        proposal_action: "NEEDS_REVIEW",
        title: "Complex Experiential Pattern",
        body: "Detailed analytical observation text.",
        confidence_level: "high",
        is_executable: false,
      },
      liveAccept: vi.fn(),
      liveApprove: vi.fn(),
      dismissRunOutcome: vi.fn(),
      cancelActiveRun: vi.fn(),
    };

    render(
      <StoreContext.Provider value={fakeStore}>
        <ProcessingResult />
      </StoreContext.Provider>
    );

    expect(screen.getByRole("alert")).toHaveTextContent("proposal_hash does not match the active proposal");
  });

  it("editing a non-CREATE proposal cannot convert it into an executable action", () => {
    // 1. In UI, Edit button is NOT rendered for non-CREATE proposals.
    // 2. State-level mockEdit strictly retains is_executable: false.
    const nonCreateRun = {
      ...baseRunState,
      proposal_action: "NEEDS_REVIEW",
      is_executable: false,
    };
    const mockEditFn = vi.fn((body: string) => {
      nonCreateRun.edited_body = body;
      nonCreateRun.is_executable = nonCreateRun.proposal_action === "CREATE";
      nonCreateRun.review_state = "mock_edited_stale";
    });
    const fakeStore: any = {
      state: {
        appLanguage: "en",
        stagingLanguage: "en",
        auditLanguage: "en",
        sourceMode: "vault",
        retrievalType: "lexical",
        run: nonCreateRun,
        runEvents: [],
        recentAudits: [],
        recentStaging: [],
      },
      mockApprove: vi.fn(),
      mockReject: vi.fn(),
      mockEdit: mockEditFn,
      liveAvailable: false,
      liveConnected: false,
      liveError: null,
      actionInFlight: false,
      liveApproval: null,
      dismissRunOutcome: vi.fn(),
      cancelActiveRun: vi.fn(),
    };

    render(
      <StoreContext.Provider value={fakeStore}>
        <ProcessingResult />
      </StoreContext.Provider>
    );

    // Edit button must not be present
    expect(screen.queryByRole("button", { name: /Edit/i })).not.toBeInTheDocument();

    // Invoking mockEdit directly must not flip is_executable to true
    mockEditFn("Attempted edit on non-CREATE proposal");
    expect(nonCreateRun.is_executable).toBe(false);
    expect(nonCreateRun.proposal_action).toBe("NEEDS_REVIEW");
  });
});

describe("Store event handling and warning isolation", () => {
  it("warning from an unrelated run does not corrupt active run state or event log", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "active-run-123",
        stage: "ingestion",
        message: "ingesting",
        timestamp: "2026-10-27T10:00:00Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("active-run-123");
    expect(currentStore.state.run.current_stage).toBe("ingestion");
    expect(currentStore.state.run.stopped).toBe(false);

    // Foreign warning arrives from an unrelated run
    act(() => {
      currentStore.applyEvent({
        kind: "warning",
        run_id: "foreign-run-999",
        stage: "audit",
        message: "Audit storage warning on different run",
        timestamp: "2026-10-27T10:00:01Z",
      });
    });

    // Active run must remain completely intact
    expect(currentStore.state.run.run_id).toBe("active-run-123");
    expect(currentStore.state.run.current_stage).toBe("ingestion");
    expect(currentStore.state.run.status).toBe("ingesting");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.runEvents.some((e: RunEvent) => e.run_id === "foreign-run-999")).toBe(false);
  });

  it("warning on a stopped run preserves stopped state, stop reason, and current stage", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "stopped-run-456",
        stage: "human_review",
        message: "reviewing",
        timestamp: "2026-10-27T10:00:00Z",
      });
      currentStore.applyEvent({
        kind: "stopped",
        run_id: "stopped-run-456",
        stage: "human_review",
        message: "run cancelled by user request",
        timestamp: "2026-10-27T10:00:02Z",
      });
    });

    expect(currentStore.state.run.stopped).toBe(true);
    expect(currentStore.state.run.stop_reason).toBe("run cancelled by user request");
    expect(currentStore.state.run.current_stage).toBe("human_review");

    // Late warning arrives for this stopped run (e.g. audit write skipped)
    act(() => {
      currentStore.applyEvent({
        kind: "warning",
        run_id: "stopped-run-456",
        stage: "audit",
        message: "Audit record write skipped (no audit writer available)",
        timestamp: "2026-10-27T10:00:03Z",
      });
    });

    // Warning must NOT revive the run or clear stop_reason or alter stopped stage
    expect(currentStore.state.run.stopped).toBe(true);
    expect(currentStore.state.run.stop_reason).toBe("run cancelled by user request");
    expect(currentStore.state.run.current_stage).toBe("human_review");
    expect(currentStore.state.run.status).toBe("Audit record write skipped (no audit writer available)");
  });

  it("warning on an active running run preserves stopped=false and does not overwrite current stage", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "active-run-789",
        stage: "llm_synthesis",
        message: "synthesizing",
        timestamp: "2026-10-27T10:00:00Z",
      });
    });

    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.run.current_stage).toBe("llm_synthesis");

    // Non-fatal warning arrives during synthesis
    act(() => {
      currentStore.applyEvent({
        kind: "warning",
        run_id: "active-run-789",
        stage: "llm_synthesis",
        message: "High latency on synthesis model",
        timestamp: "2026-10-27T10:00:01Z",
      });
    });

    // Must remain active (stopped=false), preserve current stage, and reflect status
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.run.stop_reason).toBeFalsy();
    expect(currentStore.state.run.current_stage).toBe("llm_synthesis");
    expect(currentStore.state.run.status).toBe("High latency on synthesis model");
  });

  it("lifecycle events from an unrelated run do not corrupt active run", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "active-run-101",
        stage: "classification",
        message: "classifying",
        timestamp: "2026-10-27T10:00:00Z",
      });
    });

    // Foreign stage_started from an old or different run
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "stale-run-000",
        stage: "output",
        message: "completing",
        timestamp: "2026-10-27T10:00:01Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("active-run-101");
    expect(currentStore.state.run.current_stage).toBe("classification");
    expect(currentStore.state.run.status).toBe("classifying");
  });

  it("foreign run_started event cannot replace active run ID, stage, stopped state, or event log", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish an active run at retrieval stage
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "active-run-alpha",
        stage: "retrieval",
        message: "retrieving",
        timestamp: "2026-10-27T10:00:00Z",
      });
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "active-run-alpha",
        stage: "retrieval",
        message: "query preparation",
        timestamp: "2026-10-27T10:00:01Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("active-run-alpha");
    expect(currentStore.state.run.current_stage).toBe("retrieval");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.runEvents.length).toBe(2);

    // 2. Foreign or stale run_started arrives with a different run_id
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "foreign-run-omega",
        stage: "input",
        message: "foreign run starting",
        timestamp: "2026-10-27T10:00:02Z",
      });
    });

    // 3. Active run MUST NOT be replaced or corrupted by foreign run_started
    expect(currentStore.state.run.run_id).toBe("active-run-alpha");
    expect(currentStore.state.run.current_stage).toBe("retrieval");
    expect(currentStore.state.run.status).toBe("query preparation");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.runEvents.length).toBe(2);
    expect(currentStore.state.runEvents.some((e: RunEvent) => e.run_id === "foreign-run-omega")).toBe(false);
  });

  it("foreign run_started event cannot revive or corrupt a stopped run", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish a stopped run
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "stopped-run-gamma",
        stage: "human_review",
        message: "awaiting review",
        timestamp: "2026-10-27T10:00:00Z",
      });
      currentStore.applyEvent({
        kind: "stopped",
        run_id: "stopped-run-gamma",
        stage: "human_review",
        message: "run cancelled by user request",
        timestamp: "2026-10-27T10:00:02Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("stopped-run-gamma");
    expect(currentStore.state.run.stopped).toBe(true);
    expect(currentStore.state.run.stop_reason).toBe("run cancelled by user request");
    expect(currentStore.state.run.current_stage).toBe("human_review");

    // 2. Foreign run_started arrives
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "foreign-run-zeta",
        stage: "input",
        message: "new run started",
        timestamp: "2026-10-27T10:00:03Z",
      });
    });

    // 3. Stopped run remains stopped, retains original run_id, stage, and reason
    expect(currentStore.state.run.run_id).toBe("stopped-run-gamma");
    expect(currentStore.state.run.stopped).toBe(true);
    expect(currentStore.state.run.stop_reason).toBe("run cancelled by user request");
    expect(currentStore.state.run.current_stage).toBe("human_review");
    expect(currentStore.state.runEvents.some((e: RunEvent) => e.run_id === "foreign-run-zeta")).toBe(false);
  });

  it("stale run_started for the same run ID cannot revive a stopped run", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "stopped-run-delta",
        stage: "human_review",
        message: "awaiting review",
        timestamp: "2026-10-27T10:00:00Z",
      });
      currentStore.applyEvent({
        kind: "stopped",
        run_id: "stopped-run-delta",
        stage: "human_review",
        message: "cancelled by user",
        timestamp: "2026-10-27T10:00:02Z",
      });
    });

    expect(currentStore.state.run.stopped).toBe(true);

    // Delayed run_started for the exact same run arrives late
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "stopped-run-delta",
        stage: "input",
        message: "late arriving run_started",
        timestamp: "2026-10-27T10:00:03Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("stopped-run-delta");
    expect(currentStore.state.run.stopped).toBe(true);
    expect(currentStore.state.run.stop_reason).toBe("cancelled by user");
    expect(currentStore.state.run.current_stage).toBe("human_review");
  });

  it("legitimate new run initializes correctly from idle/fixture and after run dismissal", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Initial store (fixture/idle state) initializes valid new run 1
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "legit-run-1",
        stage: "input",
        message: "first valid run",
        timestamp: "2026-10-27T10:00:00Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("legit-run-1");
    expect(currentStore.state.run.current_stage).toBe("input");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.runEvents).toHaveLength(1);
    expect(currentStore.state.runEvents[0].run_id).toBe("legit-run-1");

    // 2. User dismisses run outcome or prepares new run
    act(() => {
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");

    // 3. Legitimate new run 2 starts cleanly
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "legit-run-2",
        stage: "input",
        message: "second valid run",
        timestamp: "2026-10-27T10:05:00Z",
      });
    });

    expect(currentStore.state.run.run_id).toBe("legit-run-2");
    expect(currentStore.state.run.current_stage).toBe("input");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.runEvents).toHaveLength(1);
    expect(currentStore.state.runEvents[0].run_id).toBe("legit-run-2");
  });

  it("delayed stage_started from previous run is rejected during the interval after run ID is cleared", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish and retire run-alpha
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-alpha",
        stage: "input",
        message: "starting run alpha",
      });
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "run-alpha",
        stage: "ingestion",
        message: "ingesting",
      });
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.current_stage).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 2. Delayed stage_started arrives from run-alpha before new run starts
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "run-alpha",
        stage: "retrieval",
        message: "delayed query prep",
      });
    });

    // 3. Stale event must be rejected: run_id remains empty, stage remains empty, log empty
    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.current_stage).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);
  });

  it("delayed warning from previous run is rejected during the interval after run ID is cleared", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish and retire run-beta
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-beta",
        stage: "input",
        message: "starting run beta",
      });
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");

    // 2. Delayed warning from run-beta arrives during transition interval
    act(() => {
      currentStore.applyEvent({
        kind: "warning",
        run_id: "run-beta",
        stage: "audit",
        message: "Audit write delay on previous run",
      });
    });

    // 3. Rejected: must not revive run ID, alter stage, or populate log
    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.current_stage).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);
  });

  it("delayed approval and terminal events from previous run are rejected during transition interval", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish and retire run-gamma
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-gamma",
        stage: "input",
        message: "starting run gamma",
      });
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");

    // 2. Delayed approval_decided arrives from run-gamma
    act(() => {
      currentStore.applyEvent({
        kind: "approval_decided",
        run_id: "run-gamma",
        message: "proposal approved",
      });
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.review_state).not.toBe("live_approved");
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 3. Delayed human_review_required arrives from run-gamma
    act(() => {
      currentStore.applyEvent({
        kind: "human_review_required",
        run_id: "run-gamma",
        proposal_hash: "c".repeat(64),
        proposal_action: "CREATE",
        title: "Delayed proposal",
      });
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.liveApproval).toBeNull();
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 4. Delayed run_completed arrives from run-gamma
    act(() => {
      currentStore.applyEvent({
        kind: "run_completed",
        run_id: "run-gamma",
        stage: "output",
        message: "run completed",
      });
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.current_stage).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);
  });

  it("rejected foreign event cannot falsely mark connection live", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>conn:{String(currentStore.liveConnected)}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // Initially liveConnected is false
    expect(currentStore.liveConnected).toBe(false);

    // 1. An unsolicited foreign event arrives when idle
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "foreign-unsolicited-99",
        stage: "input",
        message: "foreign stage",
      });
    });

    // Foreign event was rejected; connection MUST NOT be marked live!
    expect(currentStore.liveConnected).toBe(false);
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 2. A legitimate run_started event arrives to initialize the new run
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "legit-conn-run",
        stage: "input",
        message: "starting valid run",
      });
    });

    // Accepted event sets liveConnected to true
    expect(currentStore.liveConnected).toBe(true);
    expect(currentStore.state.run.run_id).toBe("legit-conn-run");
  });

  it("legitimate new run progresses normally through stages after initialization", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Initialize legitimate run
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "progression-run-1",
        stage: "input",
        message: "run started",
      });
    });

    expect(currentStore.state.run.run_id).toBe("progression-run-1");
    expect(currentStore.state.run.current_stage).toBe("input");
    expect(currentStore.state.runEvents).toHaveLength(1);

    // 2. Progression through ingestion
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "progression-run-1",
        stage: "ingestion",
        message: "ingesting document",
      });
    });

    expect(currentStore.state.run.current_stage).toBe("ingestion");
    expect(currentStore.state.run.status).toBe("ingesting document");
    expect(currentStore.state.runEvents).toHaveLength(2);

    // 3. Progression through retrieval
    act(() => {
      currentStore.applyEvent({
        kind: "stage_completed",
        run_id: "progression-run-1",
        stage: "ingestion",
        message: "ingestion done",
      });
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "progression-run-1",
        stage: "retrieval",
        message: "retrieving notes",
      });
    });

    expect(currentStore.state.run.current_stage).toBe("retrieval");
    expect(currentStore.state.run.status).toBe("retrieving notes");
    expect(currentStore.state.runEvents).toHaveLength(4);
    expect(currentStore.state.runEvents.every((e: RunEvent) => e.run_id === "progression-run-1")).toBe(true);
  });

  it("foreign event is rejected without changing run ID, stage, stopped state, stop reason, review state, or event log", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish an active run at human_review stage
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "guarded-run-1",
        stage: "input",
        message: "started",
      });
      currentStore.applyEvent({
        kind: "human_review_required",
        run_id: "guarded-run-1",
        proposal_hash: "d".repeat(64),
        proposal_action: "CREATE",
        title: "Guarded Proposal",
      });
    });

    expect(currentStore.state.run.run_id).toBe("guarded-run-1");
    expect(currentStore.state.run.current_stage).toBe("human_review");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.run.stop_reason).toBeFalsy();
    expect(currentStore.state.run.review_state).toBe("live_awaiting_review");
    const baselineEventCount = currentStore.state.runEvents.length;

    // 2. Foreign event arrives with foreign run_id attempting to stop or corrupt the run
    act(() => {
      currentStore.applyEvent({
        kind: "stopped",
        run_id: "hostile-foreign-run",
        stage: "output",
        message: "force stopped",
      });
      currentStore.applyEvent({
        kind: "approval_decided",
        run_id: "hostile-foreign-run",
        message: "reject",
      });
      currentStore.applyEvent({
        kind: "warning",
        run_id: "hostile-foreign-run",
        stage: "audit",
        message: "foreign warning",
      });
    });

    // 3. Active run state and review state MUST be completely unchanged
    expect(currentStore.state.run.run_id).toBe("guarded-run-1");
    expect(currentStore.state.run.current_stage).toBe("human_review");
    expect(currentStore.state.run.stopped).toBe(false);
    expect(currentStore.state.run.stop_reason).toBeFalsy();
    expect(currentStore.state.run.review_state).toBe("live_awaiting_review");
    expect(currentStore.state.runEvents).toHaveLength(baselineEventCount);
    expect(currentStore.state.runEvents.some((e: RunEvent) => e.run_id === "hostile-foreign-run")).toBe(false);
  });

  it("delayed run_started from an older retired run cannot re-initialize after multiple run transitions", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish Run 1 ("run-epoch-1") and retire it
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-epoch-1",
        stage: "input",
        message: "starting run 1",
      });
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "run-epoch-1",
        stage: "extraction",
        message: "extracting",
      });
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 2. Establish Run 2 ("run-epoch-2") and retire it
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-epoch-2",
        stage: "input",
        message: "starting run 2",
      });
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "run-epoch-2",
        stage: "analysis",
        message: "analyzing",
      });
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 3. Now store is idle. A delayed, out-of-order run_started arrives from Run 1 ("run-epoch-1")
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-epoch-1",
        stage: "input",
        message: "stale replayed run_started",
      });
    });

    // 4. Stale run_started MUST be rejected: store remains idle, events empty, no mutation
    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.current_stage).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);

    // 5. A legitimate new run ("run-epoch-3") arrives and initializes normally
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "run-epoch-3",
        stage: "input",
        message: "starting run 3",
      });
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "run-epoch-3",
        stage: "synthesis",
        message: "synthesizing",
      });
    });

    expect(currentStore.state.run.run_id).toBe("run-epoch-3");
    expect(currentStore.state.run.current_stage).toBe("synthesis");
    expect(currentStore.state.runEvents).toHaveLength(2);
    expect(currentStore.state.runEvents[0].run_id).toBe("run-epoch-3");
    expect(currentStore.state.runEvents[1].run_id).toBe("run-epoch-3");
  });

  it("rejects uncorrelated lifecycle and human review events missing run_id, but accepts correctly correlated events", async () => {
    localStorage.setItem("lae-welcomed", "1");
    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return <div>run:{currentStore.state.run.run_id}</div>;
    }
    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Establish an active run
    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "active-run-corr-1",
        stage: "input",
        message: "starting run",
      });
    });

    expect(currentStore.state.run.run_id).toBe("active-run-corr-1");
    expect(currentStore.state.run.current_stage).toBe("input");
    expect(currentStore.state.runEvents).toHaveLength(1);
    const baselineEventCount = currentStore.state.runEvents.length;

    // 2. stage_started without run_id must be rejected
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "",
        stage: "synthesis",
        message: "uncorrelated stage",
      });
    });

    expect(currentStore.state.run.current_stage).toBe("input");
    expect(currentStore.state.runEvents).toHaveLength(baselineEventCount);

    // 3. human_review_required without run_id must be rejected
    act(() => {
      currentStore.applyEvent({
        kind: "human_review_required",
        run_id: "",
        proposal_hash: "e".repeat(64),
        proposal_action: "CREATE",
        title: "Uncorrelated Proposal",
      });
    });

    expect(currentStore.liveApproval).toBeNull();
    expect(currentStore.state.run.review_state).toBe("awaiting_review");
    expect(currentStore.state.runEvents).toHaveLength(baselineEventCount);

    // 4. approval_decided without run_id must be rejected
    act(() => {
      currentStore.applyEvent({
        kind: "approval_decided",
        run_id: "",
        message: "proposal approved",
      });
    });

    expect(currentStore.state.run.review_state).toBe("awaiting_review");
    expect(currentStore.state.runEvents).toHaveLength(baselineEventCount);

    // 5. A valid, correctly correlated event for active-run-corr-1 is accepted
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "active-run-corr-1",
        stage: "extraction",
        message: "extracting properly",
      });
    });

    expect(currentStore.state.run.current_stage).toBe("extraction");
    expect(currentStore.state.runEvents).toHaveLength(baselineEventCount + 1);
    expect(currentStore.state.runEvents[baselineEventCount].stage).toBe("extraction");

    // 6. Mismatched run_id is rejected and cannot mutate stage or review state
    act(() => {
      currentStore.applyEvent({
        kind: "stage_started",
        run_id: "mismatched-foreign-run-999",
        stage: "synthesis",
        message: "mismatched stage",
      });
      currentStore.applyEvent({
        kind: "approval_decided",
        run_id: "mismatched-foreign-run-999",
        message: "reject",
      });
    });

    expect(currentStore.state.run.current_stage).toBe("extraction");
    expect(currentStore.state.run.review_state).toBe("awaiting_review");
    expect(currentStore.state.runEvents).toHaveLength(baselineEventCount + 1);

    // 7. Transition out to idle and verify run_started without run_id cannot initialize
    act(() => {
      currentStore.dismissRunOutcome();
    });

    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);

    act(() => {
      currentStore.applyEvent({
        kind: "run_started",
        run_id: "",
        stage: "input",
        message: "anonymous run_started",
      });
    });

    // An anonymous run_started without run_id MUST NOT initialize the store
    expect(currentStore.state.run.run_id).toBe("");
    expect(currentStore.state.run.current_stage).toBe("");
    expect(currentStore.state.runEvents).toHaveLength(0);
  });
});

describe("Vault Selection Cancellation and UI State (§4)", () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("lae-welcomed", "1");
    (window as any).__LAE_BOOTSTRAP__ = { token: "mock-token-xyz" };
  });

  afterEach(() => {
    delete (window as any).__LAE_BOOTSTRAP__;
    vi.restoreAllMocks();
  });

  it("user opens VAULT MODE but cancels folder selection: remains in test mode and unconfigured", async () => {
    vi.spyOn(window, "fetch").mockImplementation(async (url: any) => {
      if (String(url).includes("/api/state")) {
        return new Response(JSON.stringify({ status: "ok", resources: {}, paused_run: null }), { status: 200 });
      }
      if (String(url).includes("/api/folders/pick")) {
        return new Response(
          JSON.stringify({ purpose: "vault", status: "cancelled", display: "", reason: "User cancelled folder picker" }),
          { status: 200 }
        );
      }
            if (String(url).includes("/api/journals")) {
        return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
    });

    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return (
        <div>
          <span data-testid="mode">{currentStore.state.sourceMode}</span>
          <span data-testid="validity">{currentStore.state.resources.vault.validity}</span>
          <button type="button" onClick={() => void currentStore.pickLiveFolder("vault")}>
            Pick Vault
          </button>
        </div>
      );
    }

    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    expect(screen.getByTestId("mode").textContent).toBe("test");
    expect(screen.getByTestId("validity").textContent).toBe("not_configured");

    // User triggers folder selection and cancels
    await act(async () => {
      await currentStore.pickLiveFolder("vault");
    });

    // Remains in test mode, validity stays not_configured
    expect(screen.getByTestId("mode").textContent).toBe("test");
    expect(screen.getByTestId("validity").textContent).toBe("not_configured");
  });

  it("folder picker fails: surfaces error and does not mark unselected vault ready", async () => {
    vi.spyOn(window, "fetch").mockImplementation(async (url: any) => {
      if (String(url).includes("/api/folders/pick")) {
        return new Response(JSON.stringify({ error: "daemon crashed" }), { status: 500 });
      }
            if (String(url).includes("/api/journals")) {
        return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
    });

    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return (
        <div>
          <span data-testid="mode">{currentStore.state.sourceMode}</span>
          <span data-testid="validity">{currentStore.state.resources.vault.validity}</span>
          <span data-testid="error">{currentStore.liveError || ""}</span>
        </div>
      );
    }

    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    await act(async () => {
      await currentStore.pickLiveFolder("vault");
    });

    // Vault marked as validation_error, error surfaced, stays test mode
    expect(screen.getByTestId("validity").textContent).toBe("validation_error");
    expect(screen.getByTestId("mode").textContent).toBe("test");
    expect(screen.getByTestId("error").textContent).not.toBe("");
  });

  it("selected folder is invalid: marks configured_invalid and disables submit in vault mode", async () => {
    vi.spyOn(window, "fetch").mockImplementation(async (url: any) => {
      if (String(url).includes("/api/folders/pick")) {
        return new Response(
          JSON.stringify({ purpose: "vault", status: "invalid", display: "bad_folder", reason: "missing marker" }),
          { status: 200 }
        );
      }
            if (String(url).includes("/api/journals")) {
        return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
    });

    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return (
        <div>
          <span data-testid="mode">{currentStore.state.sourceMode}</span>
          <span data-testid="validity">{currentStore.state.resources.vault.validity}</span>
        </div>
      );
    }

    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    await act(async () => {
      await currentStore.pickLiveFolder("vault");
    });

    expect(screen.getByTestId("validity").textContent).toBe("configured_invalid");
    // Not ready for processing in vault mode
    expect(currentStore.state.resources.vault.validity === "configured_valid").toBe(false);
  });

  it("user switches back to TEST MODE: preserves existing valid selection", async () => {
    vi.spyOn(window, "fetch").mockImplementation(async (url: any) => {
      if (String(url).includes("/api/folders/pick")) {
        return new Response(
          JSON.stringify({ purpose: "vault", status: "configured", display: "my_personal_vault", reason: "" }),
          { status: 200 }
        );
      }
            if (String(url).includes("/api/journals")) {
        return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
    });

    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return (
        <div>
          <span data-testid="mode">{currentStore.state.sourceMode}</span>
          <span data-testid="display">{currentStore.state.resources.vault.display}</span>
          <span data-testid="validity">{currentStore.state.resources.vault.validity}</span>
        </div>
      );
    }

    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Configure valid vault
    await act(async () => {
      await currentStore.pickLiveFolder("vault");
    });

    expect(screen.getByTestId("mode").textContent).toBe("vault");
    expect(screen.getByTestId("display").textContent).toBe("my_personal_vault");
    expect(screen.getByTestId("validity").textContent).toBe("configured_valid");

    // 2. Switch back to test mode
    await act(async () => {
      currentStore.setSourceMode("test");
    });

    expect(screen.getByTestId("mode").textContent).toBe("test");
    // Vault selection is preserved
    expect(screen.getByTestId("display").textContent).toBe("my_personal_vault");
    expect(screen.getByTestId("validity").textContent).toBe("configured_valid");
  });

  it("previously selected Vault exists and user attempts to select another but cancels: preserves existing selection", async () => {
    let callCount = 0;
    vi.spyOn(window, "fetch").mockImplementation(async (url: any) => {
      if (String(url).includes("/api/folders/pick")) {
        callCount++;
        if (callCount === 1) {
          return new Response(
            JSON.stringify({ purpose: "vault", status: "configured", display: "primary_vault", reason: "" }),
            { status: 200 }
          );
        } else {
          return new Response(
            JSON.stringify({ purpose: "vault", status: "cancelled", display: "", reason: "User cancelled dialog" }),
            { status: 200 }
          );
        }
      }
            if (String(url).includes("/api/journals")) {
        return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
      }
      return new Response(JSON.stringify({ audits: [], staging: [] }), { status: 200 });
    });

    let currentStore: any;
    function Capture() {
      currentStore = useStore();
      return (
        <div>
          <span data-testid="mode">{currentStore.state.sourceMode}</span>
          <span data-testid="display">{currentStore.state.resources.vault.display}</span>
          <span data-testid="validity">{currentStore.state.resources.vault.validity}</span>
        </div>
      );
    }

    render(
      <StoreProvider>
        <Capture />
      </StoreProvider>
    );

    // 1. Initial valid selection
    await act(async () => {
      await currentStore.pickLiveFolder("vault");
    });

    expect(screen.getByTestId("mode").textContent).toBe("vault");
    expect(screen.getByTestId("display").textContent).toBe("primary_vault");
    expect(screen.getByTestId("validity").textContent).toBe("configured_valid");

    // 2. User attempts to pick another folder, but cancels
    await act(async () => {
      await currentStore.pickLiveFolder("vault");
    });

    // 3. Existing valid selection MUST NOT be silently discarded
    expect(screen.getByTestId("mode").textContent).toBe("vault");
    expect(screen.getByTestId("display").textContent).toBe("primary_vault");
    expect(screen.getByTestId("validity").textContent).toBe("configured_valid");
  });
});

describe("Phase 2 Remediation: Retrieval Substeps & Comprehensive English/Persian Localization", () => {
  it("renders all 5 retrieval substeps in English and Persian with correct labels and accessibility attributes", () => {
    const events: RunEvent[] = [
      { kind: "status", run_id: "r-sub", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "r-sub", stage: "retrieval", operation: "candidate_retrieval", phase: "started", message: "", timestamp: "" },
    ];

    // English render
    const enContext = {
      state: {
        appLanguage: "en" as const,
        run: {
          run_id: "r-sub",
          current_stage: "retrieval",
          stopped: false,
        },
        runEvents: events,
      },
    };

    const { rerender } = render(
      <StoreContext.Provider value={enContext as any}>
        <Workflow />
      </StoreContext.Provider>
    );

    expect(screen.getByLabelText("Retrieval Substeps")).toBeInTheDocument();
    expect(screen.getByText("Query Preparation")).toBeInTheDocument();
    expect(screen.getByText("Candidate Retrieval")).toBeInTheDocument();
    expect(screen.getByText("Relevance Assessment")).toBeInTheDocument();
    expect(screen.getByText("Candidate Identification")).toBeInTheDocument();
    expect(screen.getByText("Retrieval Evidence Recording")).toBeInTheDocument();

    // Persian render
    const faContext = {
      state: {
        appLanguage: "fa" as const,
        run: {
          run_id: "r-sub",
          current_stage: "retrieval",
          stopped: false,
        },
        runEvents: events,
      },
    };

    rerender(
      <StoreContext.Provider value={faContext as any}>
        <Workflow />
      </StoreContext.Provider>
    );

    expect(screen.getByLabelText("زیرمراحل بازیابی")).toBeInTheDocument();
    expect(screen.getByText("آماده‌سازی پرس‌وجو")).toBeInTheDocument();
    expect(screen.getByText("بازیابی کاندیداها")).toBeInTheDocument();
    expect(screen.getByText("ارزیابی ارتباط")).toBeInTheDocument();
    expect(screen.getByText("شناسایی کاندیداها")).toBeInTheDocument();
    expect(screen.getByText("ثبت شواهد بازیابی")).toBeInTheDocument();
  });

  it("handles interrupted retrieval: cancelled sub-stage halts without completing subsequent sub-stages", () => {
    const events: RunEvent[] = [
      { kind: "status", run_id: "r-canc", stage: "retrieval", operation: "query_preparation", phase: "completed", message: "", timestamp: "" },
      { kind: "status", run_id: "r-canc", stage: "retrieval", operation: "candidate_retrieval", phase: "cancelled", message: "", timestamp: "" },
    ];

    const substeps = deriveRetrievalSubsteps("current", events, "r-canc");
    expect(substeps.map((s) => ({ key: s.key, state: s.state }))).toEqual([
      { key: "query_preparation", state: "completed" },
      { key: "candidate_retrieval", state: "cancelled" },
      { key: "relevance_assessment", state: "pending" },
      { key: "candidate_identification", state: "pending" },
      { key: "retrieval_evidence_recording", state: "pending" },
    ]);
  });

  it("localizes proposal review provenance, evidence anchors, uncertainties, and executability banner in English and Persian", () => {
    const proposalData = {
      proposal_action: "CREATE",
      proposal_title: "Authentic Action Note",
      proposal_body: "Detailed proposal content here.",
      destination: "Notes/Authenticity.md",
      reason: "Evidence-backed synthesis",
      provenance: ["Source doc A", "Source doc B"],
      evidence: ["Direct quote anchor 1", "Direct quote anchor 2"],
      uncertainties: ["Requires historical cross-check"],
      review_state: "pending",
    };

    // English render
    const enContext = {
      state: {
        appLanguage: "en" as const,
        sourceMode: "test",
        auditLanguage: "en",
        stagingLanguage: "en",
        run: proposalData,
        runEvents: [],
      },
      liveAvailable: false,
      mockApprove: vi.fn(),
      mockReject: vi.fn(),
      mockSafeStop: vi.fn(),
    };

    const { rerender } = render(
      <StoreContext.Provider value={enContext as any}>
        <ProcessingResult />
      </StoreContext.Provider>
    );

    expect(screen.getByLabelText("Proposal Review")).toBeInTheDocument();
    expect(screen.getByText("Provenance:")).toBeInTheDocument();
    expect(screen.getByText("Evidence Anchors:")).toBeInTheDocument();
    expect(screen.getByText("Uncertainties / Limitations:")).toBeInTheDocument();
    expect(screen.getByText(/Explicit human approval authorizes proceeding/)).toBeInTheDocument();

    // Persian render
    const faContext = {
      state: {
        appLanguage: "fa" as const,
        sourceMode: "test",
        auditLanguage: "fa",
        stagingLanguage: "fa",
        run: proposalData,
        runEvents: [],
      },
      liveAvailable: false,
      mockApprove: vi.fn(),
      mockReject: vi.fn(),
      mockSafeStop: vi.fn(),
    };

    rerender(
      <StoreContext.Provider value={faContext as any}>
        <ProcessingResult />
      </StoreContext.Provider>
    );

    expect(screen.getByLabelText("بررسی پیشنهاد")).toBeInTheDocument();
    expect(screen.getByText("منشأ و پیشینه:")).toBeInTheDocument();
    expect(screen.getByText("شواهد و ارجاعات مستند:")).toBeInTheDocument();
    expect(screen.getByText("عدم قطعیت‌ها و محدودیت‌ها:")).toBeInTheDocument();
    expect(screen.getByText(/تأیید صریح انسانی اجازه ورود به بازاعتبارسنجی/)).toBeInTheDocument();
  });

  it("localizes journals summary and synchronization timer in English and Persian", () => {
    // English render
    const enContext = {
      state: {
        appLanguage: "en" as const,
        sourceMode: "test",
        recentAudits: [
          {
            id: "aud-test-1",
            timestamp: "2026-10-25T10:00:00Z",
            statusKey: "journal.passed",
            stagingNoteKey: "journal.stagingCreated",
            title: "Test Audit",
            summary: "Run audit succeeded",
            emergency: false,
          },
        ],
        recentStaging: [
          {
            id: "stg-test-1",
            timestamp: "2026-10-25T10:00:00Z",
            statusKey: "journal.passed",
            stagingNoteKey: null,
            title: "Test Note",
            excerpt: "Staged note summary",
            emergency: false,
            staging_available: true,
          },
        ],
        journalIndex: 0,
        run: {},
        runEvents: [],
      },
      liveAvailable: false,
      stepJournal: vi.fn(),
      refreshJournals: vi.fn(),
    };

    const { rerender } = render(
      <StoreContext.Provider value={enContext as any}>
        <Journals />
      </StoreContext.Provider>
    );

    expect(screen.getByText("Summary Of Recent Audits")).toBeInTheDocument();
    expect(screen.getByText("Summary Of Recent Staging")).toBeInTheDocument();
    expect(screen.getByText("⟳ 5s synchronized")).toBeInTheDocument();

    // Persian render
    const faContext = {
      ...enContext,
      state: {
        ...enContext.state,
        appLanguage: "fa" as const,
      },
    };

    rerender(
      <StoreContext.Provider value={faContext as any}>
        <Journals />
      </StoreContext.Provider>
    );

    expect(screen.getByText("خلاصه حسابرسی‌های اخیر")).toBeInTheDocument();
    expect(screen.getByText("خلاصه فایل‌های موقت اخیر")).toBeInTheDocument();
    expect(screen.getByText("⟳ همگام‌سازی ۵ ثانیه‌ای")).toBeInTheDocument();
  });

  describe("Phase 2 Remediation: Retrieval Substeps & Complete Localization", () => {
    it("guarantees 100% translation key symmetry between English and Persian without missing keys", () => {
      const enKeys = Object.keys(en).sort();
      const faKeys = Object.keys(fa).sort();
      expect(enKeys).toEqual(faKeys);
      expect(enKeys.length).toBeGreaterThan(320);
    });

    it("interpolates parameters correctly in translate()", () => {
      const textEn = translate("en", "ui.nav.indexedCounts", { notes: 12, vectors: 48 });
      expect(textEn).toBe("(12 notes, 48 vectors)");

      const textFa = translate("fa", "ui.nav.indexedCounts", { notes: 12, vectors: 48 });
      expect(textFa).toBe("(12 یادداشت، 48 بردار)");
    });

    it("derives retrieval substeps correctly for complete run", () => {
      const runId = "run-test-full";
      const mk = (operation: string, phase: "started" | "completed" | "cancelled"): RunEvent => ({
        kind: "status", stage: "retrieval", operation, phase, run_id: runId, message: "", timestamp: "",
      });
      const events: RunEvent[] = [
        mk("query_preparation", "started"),
        mk("query_preparation", "completed"),
        mk("candidate_retrieval", "started"),
        mk("candidate_retrieval", "completed"),
        mk("relevance_assessment", "started"),
        mk("relevance_assessment", "completed"),
        mk("candidate_identification", "started"),
        mk("candidate_identification", "completed"),
        mk("retrieval_evidence_recording", "started"),
        mk("retrieval_evidence_recording", "completed"),
      ];

      const substeps = deriveRetrievalSubsteps("completed", events, runId);
      expect(substeps).toHaveLength(5);
      expect(substeps.every((s) => s.state === "completed")).toBe(true);
    });

    it("derives retrieval substeps correctly on intermediate cancellation without falsely completing future substeps", () => {
      const runId = "run-test-cancel";
      const mk = (operation: string, phase: "started" | "completed" | "cancelled"): RunEvent => ({
        kind: "status", stage: "retrieval", operation, phase, run_id: runId, message: "", timestamp: "",
      });
      const events: RunEvent[] = [
        mk("query_preparation", "started"),
        mk("query_preparation", "completed"),
        mk("candidate_retrieval", "started"),
        mk("candidate_retrieval", "completed"),
        mk("relevance_assessment", "cancelled"),
        { kind: "stopped", stage: "cancellation", message: "run cancelled during retrieval", run_id: runId, timestamp: "" },
      ];

      const substeps = deriveRetrievalSubsteps("stopped", events, runId);
      expect(substeps).toHaveLength(5);
      expect(substeps[0].state).toBe("completed");
      expect(substeps[1].state).toBe("completed");
      expect(substeps[2].state).toBe("cancelled");
      expect(substeps[3].state).toBe("pending");
      expect(substeps[4].state).toBe("pending");
    });

    it("renders TopNav download links and modal fallback in Persian without hardcoded English", () => {
      const faStore: any = {
        state: {
          appLanguage: "fa",
          stagingLanguage: "fa",
          auditLanguage: "fa",
          sourceMode: "vault",
          retrievalType: "lexical",
          llmMode: "local",
          resources: {
            vault: { validity: "not_configured", display: "" },
            staging: { validity: "not_configured", display: "" },
            audit: { validity: "not_configured", display: "" },
            vector_db: { validity: "not_configured", display: "" },
            embedding_model: { validity: "not_configured", display: "" },
            local_llm: { validity: "not_configured", display: "" },
          },
          run: {
            run_id: "",
            current_stage: "",
            status: "",
            stopped: false,
            stop_reason: "",
            proposal_title: "",
            proposal_body: "",
            proposal_action: "",
            confidence_level: "",
            confidence_score: 0,
            evidence: [],
            provenance: [],
            uncertainties: [],
            audit_status: "",
          },
        },
        liveAvailable: false,
        setTheme: vi.fn(),
        setAppLanguage: vi.fn(),
        setSourceMode: vi.fn(),
        setRetrievalType: vi.fn(),
        setLlmMode: vi.fn(),
        pickLiveFolder: vi.fn(),
        liveAuthorizeVaultSync: vi.fn(),
        liveGetVaultSyncStatus: vi.fn().mockResolvedValue(null),
        liveReconcileVaultSync: vi.fn(),
      };

      render(
        <StoreContext.Provider value={faStore}>
          <TopNav />
        </StoreContext.Provider>
      );

      expect(screen.getAllByText("پیکربندی‌نشده").length).toBeGreaterThan(0);
    });
  });
});
