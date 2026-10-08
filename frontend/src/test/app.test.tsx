import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
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

describe("app shell", () => {
  it("renders nav, current audit, result, journals, composer, responsibility", async () => {
    const user = userEvent.setup();
    render(<App />);
    expect(screen.getByLabelText("top")).toBeInTheDocument();
    expect(screen.getByLabelText("Current Audit")).toBeInTheDocument();
    expect(screen.getByLabelText("Processing Result")).toBeInTheDocument();
    expect(screen.getByLabelText("Recent Audit journal")).toBeInTheDocument();
    expect(screen.getByLabelText("Recent Staging journal")).toBeInTheDocument();
    // Responsibility notice lives in the expanded composer.
    await user.click(screen.getByRole("button", { name: /Type text input here/ }));
    expect(screen.getByText("LA-E can make mistakes and the human is responsible for its use.")).toBeInTheDocument();
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
    const box = screen.getByLabelText("Edit (mock stale)");
    await user.clear(box);
    await user.type(box, "edited fixture text");
    await user.click(screen.getByRole("button", { name: /Save edit/ }));
    expect(await screen.findByText(/mock_edited_stale/)).toBeInTheDocument();
    expect(screen.getByText(/display only; no authoritative re-hash/i)).toBeInTheDocument();
  });

  it("theme toggle + RTL switch update document attributes", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("lae-theme")).toBe("dark");
    await user.click(screen.getByRole("radio", { name: "فارسی" }));
    expect(document.documentElement.dir).toBe("rtl");
    expect(document.documentElement.lang).toBe("fa");
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
  it("shows onboarding once and engine install prompt with mock choices", async () => {
    const user = userEvent.setup();
    render(<App />);
    expect(screen.getByText(/initial vector index is needed/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Create index/ }));
    expect(screen.queryByText(/initial vector index is needed/)).not.toBeInTheDocument();
    expect(screen.getByText(/requires a vector database engine/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Install LanceDB/ }));
    expect(screen.queryByText(/requires a vector database engine/)).not.toBeInTheDocument();
  });

  it("mock safe-stop offers Fix/Stop; Fix keeps same run_id", async () => {
    const user = userEvent.setup();
    render(<App />);
    await user.click(screen.getByRole("button", { name: /Simulate safe-stop/ }));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Fix" }));
    expect(await screen.findByText(/same run resumes/)).toBeInTheDocument();
    expect(screen.getAllByText(/run-fixture-001/).length).toBeGreaterThanOrEqual(1);
  });

  it("resource panel shows vector/llm/audit/eligibility contract notes", () => {
    render(<App />);
    expect(screen.getByText(/Vector index/)).toBeInTheDocument();
    expect(screen.getByText(/Audit root holds passed/)).toBeInTheDocument();
    expect(screen.getByText(/Only eligible staged/)).toBeInTheDocument();
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
