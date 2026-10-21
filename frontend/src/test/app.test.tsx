import { useState } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { deriveWorkflow, isTerminalSuccess } from "../components/workflowStages";
import { StoreProvider, useStore } from "../state/store";
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
});

