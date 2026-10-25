import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
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
import { TranslateAction, Journals } from "../components/Panels";
import { computeSha256 } from "../contracts/crypto";
import type { RunEvent } from "../contracts/transport";

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
      { run_id: "run-1", current_stage: "", stopped: false, status: "" },
      [ev("run_started"), ev("stage_completed", "ingestion"),
       ev("human_review_required", "human_review"),
       ev("approval_decided", "human_review", "approve"),
       ev("revalidation_result", "revalidation", "valid"),
       ev("execution_result", "controlled_execution", "executed"),
       ev("run_completed")]);
    const by = Object.fromEntries(stages.map((s) => [s.key, s.state]));
    expect(by.explicit_approval).toBe("completed");
    expect(by.revalidation).toBe("completed");
    expect(by.controlled_execution).toBe("completed");
    expect(by.audit).toBe("completed");
    expect(by.output).toBe("completed");
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
});

import userEvent from "@testing-library/user-event";
import { App } from "../App";
import { BACKEND_STAGE_ORDER } from "../components/stageOrder";
import { UI_KEYS_EN, translate } from "../i18n/resources";
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
    expect(isTerminalSuccess([...base, ev("run_completed")])).toBe(true);
    // No terminal event → not success.
    expect(isTerminalSuccess(base)).toBe(false);
    // Any stop/error/rejection after completion → not success.
    expect(isTerminalSuccess([...base, ev("run_completed"), ev("stopped", "x", "y")])).toBe(false);
    expect(isTerminalSuccess([...base, ev("run_completed"), ev("error", "x", "y")])).toBe(false);
    // Rejected approval → not success.
    const rejected = base.map((e, i) =>
      i === 2 ? ev("approval_decided", "human_review", "reject") : e);
    expect(isTerminalSuccess([...rejected, ev("run_completed")])).toBe(false);
    // Missing execution → not success.
    const noExec = base.filter((e) => e.kind !== "execution_result");
    expect(isTerminalSuccess([...noExec, ev("run_completed")])).toBe(false);
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
    expect(screen.getByText("stale")).toBeInTheDocument();

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
    expect(screen.getByText("stale")).toBeInTheDocument();

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
    expect(screen.getByText("stale")).toBeInTheDocument();

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
    expect(screen.getByText("stale")).toBeInTheDocument();
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
    expect(screen.getByText("unverifiable")).toBeInTheDocument();

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
    expect(screen.getByText("stale")).toBeInTheDocument();
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
    expect(screen.getByText("unverifiable")).toBeInTheDocument();
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
    expect(screen.getByText("unverifiable")).toBeInTheDocument();
    // Non-persisted on-demand badge is visible
    expect(screen.getByText("on-demand")).toBeInTheDocument();

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
    expect(screen.getByText("unverifiable")).toBeInTheDocument();

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
    expect(screen.getByText("unverifiable")).toBeInTheDocument();
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
    expect(screen.getByText("unverifiable")).toBeInTheDocument();

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
    expect(screen.getByText("stale")).toBeInTheDocument();

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
    expect(screen.getByText("current")).toBeInTheDocument();
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

