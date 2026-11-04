/// <reference path="./node.d.ts" />
import { test, expect } from "@playwright/test";
import * as child_process from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as url from "url";
import { isProcessExited, terminateProcess, removeDirSafely } from "./process_lifecycle";

interface ServerInfo {
  url: string;
  port: number;
  token: string;
  instance_id: string;
  staging_dir: string;
  audit_dir: string;
  vault_dir?: string;
}

let serverProcess: child_process.ChildProcess | null = null;
let serverInfo: ServerInfo | null = null;
let tempTestDir: string = "";

const __dirname = url.fileURLToPath(new URL(".", import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "../..");
const PYTHON_PATH = process.env.PYTHON_PATH || path.resolve(WORKSPACE_ROOT, "Venv/Scripts/python.exe");
const AUTHORIZED_TEST_OUTPUT_DIR = path.resolve(WORKSPACE_ROOT, "tests/output/playwright_e2e");

async function startTestServer(): Promise<ServerInfo> {
  tempTestDir = path.resolve(
    AUTHORIZED_TEST_OUTPUT_DIR,
    `.e2e_run_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  );
  fs.mkdirSync(tempTestDir, { recursive: true });

  const harnessScript = path.resolve(WORKSPACE_ROOT, "tests/e2e/e2e_server_harness.py");
  const webRoot = path.resolve(WORKSPACE_ROOT, "frontend/dist");

  return new Promise((resolve, reject) => {
    const proc = child_process.spawn(
      PYTHON_PATH,
      [harnessScript, "--web-root", webRoot, "--data-dir", tempTestDir],
      {
        cwd: WORKSPACE_ROOT,
        stdio: ["pipe", "pipe", "pipe"],
      }
    );

    serverProcess = proc;
    let stdoutData = "";
    let stderrData = "";
    let started = false;
    let cleanedUp = false;

    let cleanupStartupListeners = () => {};

    const handleStartupFailure = async (reason: string, originalErr?: Error) => {
      if (cleanedUp) return;
      cleanedUp = true;
      clearTimeout(timeout);
      cleanupStartupListeners();
      serverProcess = null;

      const cleanupErrors: string[] = [];
      let processConfirmedTerminated = false;

      try {
        await terminateProcess(proc);
        processConfirmedTerminated = isProcessExited(proc);
      } catch (tErr) {
        const msg = tErr instanceof Error ? tErr.message : String(tErr);
        cleanupErrors.push(`Process termination failed: ${msg}`);
      }

      // Strictly verify process termination before directory removal
      if (processConfirmedTerminated) {
        try {
          await removeDirSafely(tempTestDir);
        } catch (rmErr) {
          const msg = rmErr instanceof Error ? rmErr.message : String(rmErr);
          cleanupErrors.push(`Directory cleanup failed: ${msg}`);
        }
      } else {
        cleanupErrors.push(
          `Directory cleanup skipped: temporary directory '${tempTestDir}' was retained to avoid mutating files while process exit remains unconfirmed.`
        );
      }

      if (cleanupErrors.length > 0) {
        const aggregatedMsg = `E2E server startup failed: ${reason}. Additionally, cleanup issues occurred: [${cleanupErrors.join(
          "; "
        )}]. Stderr: ${stderrData}`;
        reject(new Error(aggregatedMsg));
      } else {
        const failureMessage = `E2E server startup failed: ${reason}. Stderr: ${stderrData}`;
        reject(originalErr || new Error(failureMessage));
      }
    };

    const timeout = setTimeout(() => {
      if (!started) {
        void handleStartupFailure("Timeout (20s) waiting for E2E server ready signal");
      }
    }, 20000);

    proc.stdout?.on("data", (chunk: { toString(enc?: string): string }) => {
      stdoutData += chunk.toString("utf-8");
      const lines = stdoutData.split("\n");
      for (const line of lines) {
        if (line.startsWith("READY:")) {
          started = true;
          clearTimeout(timeout);
          cleanupStartupListeners();
          try {
            const parsed = JSON.parse(line.slice(6)) as ServerInfo;
            resolve(parsed);
          } catch (e) {
            void handleStartupFailure(
              `Failed to parse READY JSON: ${line}`,
              e instanceof Error ? e : new Error(String(e))
            );
          }
          return;
        }
      }
    });

    proc.stderr?.on("data", (chunk: { toString(enc?: string): string }) => {
      stderrData += chunk.toString("utf-8");
    });

    const onExit = (code: number | null) => {
      if (!started) {
        void handleStartupFailure(`Process exited prematurely with code ${code}`);
      }
    };

    const onError = (err: Error) => {
      if (!started) {
        void handleStartupFailure(`Process spawn error: ${err.message}`, err);
      }
    };

    cleanupStartupListeners = () => {
      proc.removeListener("exit", onExit);
      proc.removeListener("error", onError);
    };

    proc.on("exit", onExit);
    proc.on("error", onError);
  });
}

async function stopTestServer(): Promise<void> {
  const proc = serverProcess;
  serverProcess = null;
  const dir = tempTestDir;
  tempTestDir = "";

  if (proc) {
    await terminateProcess(proc);
  }

  if (dir) {
    if (!proc || isProcessExited(proc)) {
      await removeDirSafely(dir);
    } else {
      throw new Error(
        `Directory cleanup skipped in stopTestServer: temporary directory '${dir}' was retained because process termination could not be confirmed.`
      );
    }
  }
}

test.describe("Human Review End-to-End Browser Suite", () => {
  test.beforeAll(async () => {
    serverInfo = await startTestServer();
  });

  test.afterAll(async () => {
    await stopTestServer();
  });

  test.beforeEach(async ({ page }) => {
    // Pre-seed localStorage to bypass introductory welcome modal and tutorials
    await page.addInitScript(() => {
      localStorage.setItem("lae-welcomed", "1");
      localStorage.setItem("lae-first-vault", "1");
      localStorage.setItem("lae-first-staging", "1");
      localStorage.setItem("lae-first-audit", "1");
    });
  });

  test("CREATE Path: Submit observation -> green banner -> approve -> execution & passed audit record", async ({ page }) => {
    expect(serverInfo).not.toBeNull();
    const info = serverInfo!;

    // Navigate to local UI served by Python LauncherServer
    await page.goto(info.url);

    // Ensure title and main components are loaded
    await expect(page).toHaveTitle(/LivingAuthenticity/);

    // Verify chat composer is available
    const composerTextarea = page.locator("#chat-composer-textarea");
    await expect(composerTextarea).toBeVisible();

    // Enter empirical observation designed to trigger CREATE proposal in dynamic E2E synthesis
    const observationText =
      "Observation: People often share calm morning rituals and daily sequence repeats across community notes.";
    await composerTextarea.fill(observationText);

    // Submit the run
    const submitBtn = page.locator("button.submit-btn");
    await expect(submitBtn).toBeEnabled();
    await submitBtn.click();

    // Verify UI enters processing and proposal review card appears
    const proposalCard = page.locator(".proposal-content");
    await expect(proposalCard).toBeVisible({ timeout: 15000 });

    // Verify Executable Action banner (Green)
    const execBanner = page.locator(".proposal-executability-banner.exec-create");
    await expect(execBanner).toBeVisible();
    await expect(execBanner).toContainText("Executable Action (CREATE)");

    // Verify proposal metadata is rendered
    const titleEl = page.locator(".proposal-title");
    await expect(titleEl).toContainText("People often share calm morning rituals");

    const reasonEl = page.locator(".proposal-reason");
    await expect(reasonEl).toBeVisible();

    const detailsBox = page.locator(".proposal-details-box");
    await expect(detailsBox).toBeVisible();

    // Extract exact run_id from the proposal identity labels in the review UI
    const runIdCode = page.locator(".run-id-label code");
    await expect(runIdCode).toBeVisible();
    const runId = (await runIdCode.textContent())?.trim() ?? "";
    expect(runId).toBeTruthy();

    // Verify Approve button is present and click it
    const approveBtn = page.locator("button.btn.approve:has-text('Approve')");
    await expect(approveBtn).toBeVisible();
    await expect(approveBtn).toBeEnabled();

    await approveBtn.click();

    // Verify workflow transitions to completion
    const timeline = page.locator("ol.timeline");
    await expect(timeline).toBeVisible();

    const reviewStage = page.locator("ol.timeline li:has-text('Human Review')");
    await expect(reviewStage).toHaveAttribute("data-state", "completed", { timeout: 15000 });

    const execStage = page.locator("ol.timeline li:has-text('Controlled Execution')");
    await expect(execStage).toHaveAttribute("data-state", "completed", { timeout: 15000 });

    const auditStage = page.locator("ol.timeline li:has-text('Audit')");
    await expect(auditStage).toHaveAttribute("data-state", "completed", { timeout: 15000 });

    // Filesystem Invariant: Staged markdown note written to isolated staging directory
    const stagedFiles = fs.readdirSync(info.staging_dir).filter((f: string) => f.endsWith(".md"));
    expect(stagedFiles.length).toBeGreaterThanOrEqual(1);

    const stagedContent = fs.readFileSync(path.join(info.staging_dir, stagedFiles[0]), "utf-8");
    expect(stagedContent).toContain("Observation: People often share calm morning rituals and daily sequence repeats across community notes.");
    expect(stagedContent).toContain("## LLM synthesis");
    expect(stagedContent).toContain("People often share calm morning rituals and the pattern repeats daily.");

    // Filesystem Invariant: Durable audit record persisted in audit/passed/audit.jsonl
    const passedAuditPath = path.join(info.audit_dir, "passed", "audit.jsonl");
    expect(fs.existsSync(passedAuditPath)).toBe(true);

    const auditLines: Record<string, unknown>[] = fs
      .readFileSync(passedAuditPath, "utf-8")
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l: string) => JSON.parse(l));

    // Filter audit records by exact run_id and assert bijective receipt uniqueness (exactly one matching record)
    const matchingRecords = auditLines.filter((r) => r.run_id === runId);
    expect(matchingRecords).toHaveLength(1);

    const createRecord = matchingRecords[0];
    expect(createRecord.run_id).toBe(runId);
    expect(createRecord.proposal_action).toBe("CREATE");
    expect(createRecord.terminal_outcome).toBe("executed");
    expect(createRecord.approval_approved).toBe(true);
    expect(createRecord.execution_executed).toBe(true);
    expect(createRecord.audit_phase).toBe("post_execution");
    expect(createRecord.storage_category).toBe("passed");

    // Two-Phase Audit Contract: Pre-execution safety checkpoint in audit/failed/audit.jsonl
    const failedAuditPath = path.join(info.audit_dir, "failed", "audit.jsonl");
    expect(fs.existsSync(failedAuditPath)).toBe(true);

    const failedLines: Record<string, unknown>[] = fs
      .readFileSync(failedAuditPath, "utf-8")
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l: string) => JSON.parse(l));

    const matchingFailedRecords = failedLines.filter((r) => r.run_id === runId);
    // Exactly one matching pre-execution checkpoint exists for this run_id
    expect(matchingFailedRecords).toHaveLength(1);

    const preCheckpoint = matchingFailedRecords[0];
    expect(preCheckpoint.run_id).toBe(runId);
    expect(preCheckpoint.audit_phase).toBe("pre_execution");
    expect(preCheckpoint.terminal_outcome).toBe("approved_pending_execution");
    expect(preCheckpoint.proposal_action).toBe("CREATE");
    expect(preCheckpoint.approval_approved).toBe(true);
    expect(preCheckpoint.revalidation_performed).toBe(true);
    expect(preCheckpoint.revalidation_valid).toBe(true);
    expect(preCheckpoint.execution_attempted).toBe(false);
    expect(preCheckpoint.execution_executed).toBe(false);
    expect(preCheckpoint.storage_category).toBe("failed");
    expect(preCheckpoint.record_id).not.toBe(createRecord.record_id);
    expect(preCheckpoint.proposal_hash).toBe(createRecord.proposal_hash);

    // Strict Partition Invariant: zero unexpected records (multiple checkpoints, post-execution, or execution failures)
    const unexpectedFailedRecords = matchingFailedRecords.filter(
      (r) => r.audit_phase !== "pre_execution" || r.execution_attempted === true
    );
    expect(unexpectedFailedRecords).toHaveLength(0);
  });

  test("Non-CREATE Path: Submit text -> warning banner -> acknowledge -> zero staging & failed/held audit", async ({ page }) => {
    expect(serverInfo).not.toBeNull();
    const info = serverInfo!;

    await page.goto(info.url);

    // Enter non-CREATE observation text
    const composerTextarea = page.locator("#chat-composer-textarea");
    await expect(composerTextarea).toBeVisible();

    const nonCreateText =
      "Empirical investigation of reflective agency and ontological congruence in cognitive agents.";
    await composerTextarea.fill(nonCreateText);

    // Submit
    const submitBtn = page.locator("button.submit-btn");
    await expect(submitBtn).toBeEnabled();
    await submitBtn.click();

    // Verify UI renders non-executable warning banner
    const warningBanner = page.locator(".proposal-executability-banner.non-exec-review");
    await expect(warningBanner).toBeVisible({ timeout: 15000 });
    await expect(warningBanner).toContainText("Non-Executable Proposal (NEEDS_REVIEW)");
    await expect(warningBanner).toContainText("no execution will occur");

    // Strictly ensure NO "Approve" button is present
    const approveBtn = page.locator("button.btn.approve:has-text('Approve')");
    await expect(approveBtn).toHaveCount(0);

    // Dedicated Acknowledge button is present
    const acknowledgeBtn = page.locator("button.btn.acknowledge:has-text('Acknowledge')");
    await expect(acknowledgeBtn).toBeVisible();
    await expect(acknowledgeBtn).toBeEnabled();

    // Extract exact run_id from the proposal identity labels in the review UI
    const runIdCode = page.locator(".run-id-label code");
    await expect(runIdCode).toBeVisible();
    const runId = (await runIdCode.textContent())?.trim() ?? "";
    expect(runId).toBeTruthy();

    // Record staging directory state before acknowledgment
    const stagingBefore = fs.readdirSync(info.staging_dir).filter((f: string) => f.endsWith(".md"));

    // Click Acknowledge
    await acknowledgeBtn.click();

    // Verify UI acknowledgment confirmation
    const ackConfirmation = page.locator(".ok-text:has-text('Acknowledged')");
    await expect(ackConfirmation).toBeVisible({ timeout: 15000 });

    // Downstream execution stage must NOT be completed
    const execStage = page.locator("ol.timeline li:has-text('Controlled Execution')");
    const execState = await execStage.getAttribute("data-state");
    expect(execState).not.toBe("completed");

    // Filesystem Invariant: Strictly zero files created in staging directory
    const stagingAfter = fs.readdirSync(info.staging_dir).filter((f: string) => f.endsWith(".md"));
    expect(stagingAfter.length).toBe(stagingBefore.length);

    // Filesystem Invariant: Durable audit record in audit/failed/audit.jsonl
    const failedAuditPath = path.join(info.audit_dir, "failed", "audit.jsonl");
    expect(fs.existsSync(failedAuditPath)).toBe(true);

    const auditLines: Record<string, unknown>[] = fs
      .readFileSync(failedAuditPath, "utf-8")
      .trim()
      .split("\n")
      .filter(Boolean)
      .map((l: string) => JSON.parse(l));

    // Filter audit records by exact run_id and assert bijective receipt uniqueness (exactly one matching record)
    const matchingRecords = auditLines.filter((r) => r.run_id === runId);
    expect(matchingRecords).toHaveLength(1);

    const ackRecord = matchingRecords[0];
    expect(ackRecord.run_id).toBe(runId);
    expect(ackRecord.proposal_action).toBe("NEEDS_REVIEW");
    expect(ackRecord.human_review_state).toBe("acknowledged_non_executable");
    expect(ackRecord.approval_approved).toBe(false);
    expect(ackRecord.execution_executed).toBe(false);
    expect(ackRecord.terminal_outcome).toBe("held_non_executable");

    // Strict Partition Invariant: zero records with this run_id exist in audit/passed/audit.jsonl
    const passedAuditPath = path.join(info.audit_dir, "passed", "audit.jsonl");
    if (fs.existsSync(passedAuditPath)) {
      const passedLines: Record<string, unknown>[] = fs
        .readFileSync(passedAuditPath, "utf-8")
        .trim()
        .split("\n")
        .filter(Boolean)
        .map((l: string) => JSON.parse(l));
      const crossPartitionRecords = passedLines.filter((r) => r.run_id === runId);
      expect(crossPartitionRecords).toHaveLength(0);
    }
  });

  test("Security & Cancellation: Reject action mismatch & stopped status on cancellation before decision", async ({ page, request }) => {
    expect(serverInfo).not.toBeNull();
    const info = serverInfo!;

    // 1. Backend wire security: Contradictory action rejection (HTTP 400)
    const mismatchResp = await request.post(`${info.url}api/approve`, {
      headers: {
        "X-LAE-Token": info.token,
        "Content-Type": "application/json",
      },
      data: {
        run_id: "test-run-mismatch",
        proposal_hash: "hash-001",
        action: "CREATE",
        proposal_action: "NEEDS_REVIEW",
      },
    });
    expect(mismatchResp.status()).toBe(400);

    // 2. Missing run_id rejection (HTTP 400)
    const missingRunIdResp = await request.post(`${info.url}api/run/accept`, {
      headers: {
        "X-LAE-Token": info.token,
        "Content-Type": "application/json",
      },
      data: {
        proposal_hash: "hash-002",
      },
    });
    expect(missingRunIdResp.status()).toBe(400);

    // 3. UI Cancellation before decision marks review as stopped, never completed
    await page.goto(info.url);
    const composerTextarea = page.locator("#chat-composer-textarea");
    await composerTextarea.fill(
      "Observation: Testing run cancellation before decision. Calm morning rituals and daily sequence repeats."
    );

    const submitBtn = page.locator("button.submit-btn");
    await submitBtn.click();

    // Wait until proposal appears
    const reviewCard = page.locator(".proposal-executability-banner");
    await expect(reviewCard).toBeVisible({ timeout: 15000 });

    // Cancel the run via the UI cancel button in the current audit panel
    const cancelBtn = page.locator(".current-audit-overlay button.cancel-run-btn");
    await expect(cancelBtn).toBeVisible({ timeout: 15000 });
    await cancelBtn.click();

    // Human review stage must be marked stopped, NOT completed
    const reviewStage = page.locator("ol.timeline li:has-text('Human Review')");
    await expect(reviewStage).toHaveAttribute("data-state", "stopped", { timeout: 15000 });

    // Execution stage must remain pending / stopped, NOT completed
    const execStage = page.locator("ol.timeline li:has-text('Controlled Execution')");
    const execState = await execStage.getAttribute("data-state");
    expect(execState).not.toBe("completed");

    // Audit stage must be marked 'logged' reflecting the emergency operational record persisted on cancellation
    const auditStage = page.locator("ol.timeline li:has-text('Audit')");
    await expect(auditStage).toHaveAttribute("data-state", "logged", { timeout: 15000 });
  });

  test("VAULT MODE: User Vault Selection Through Application -> Ingest synthetic vault -> CREATE path -> execution & zero vault mutation", async ({ page }) => {
    expect(serverInfo).not.toBeNull();
    const info = serverInfo!;
    expect(info.vault_dir).toBeTruthy();

    // Snapshot vault files before run
    const readVaultState = (dir: string) => {
      const state: Record<string, string> = {};
      const walk = (d: string) => {
        for (const item of fs.readdirSync(d)) {
          const full = path.join(d, item);
          if (fs.statSync(full).isDirectory()) {
            walk(full);
          } else {
            state[full] = fs.readFileSync(full, "utf-8");
          }
        }
      };
      walk(dir);
      return state;
    };

    const initialVaultState = readVaultState(info.vault_dir!);
    expect(Object.keys(initialVaultState).length).toBeGreaterThanOrEqual(12);

    // Suppress onboarding modals in browser state
    await page.addInitScript(() => {
      localStorage.setItem("lae-first-vault", "1");
      localStorage.setItem("lae-welcome-dismissed", "1");
    });

    await page.goto(info.url);
    await expect(page).toHaveTitle(/LivingAuthenticity/);
    await expect(page.locator("#chat-composer-textarea")).toBeVisible();

    // Priority A Step 1: Prove browser opens application in unconfigured TEST MODE
    await expect(page.locator(".live-status-pill .live-dot.on")).toBeVisible({ timeout: 15000 });
    const testTab = page.locator("button.tab-test");
    await expect(testTab).toBeVisible({ timeout: 15000 });

    // Priority A Step 2: Set up response listener for POST /api/folders/pick
    const pickPromise = page.waitForResponse(
      (resp) => resp.url().includes("/api/folders/pick") && resp.request().method() === "POST",
      { timeout: 15000 }
    );

    // Priority A Step 3: Invoke user-facing Vault folder-selection control
    await testTab.click();

    // Priority A Step 4: Verify actual folder-selection endpoint returned the authorized synthetic Vault
    const pickResponse = await pickPromise;
    expect(pickResponse.status()).toBe(200);
    const pickJson = (await pickResponse.json()) as { status: string; display: string };
    expect(pickJson.status).toBe("configured");
    expect(pickJson.display).toBe("synthetic_vault");

    // Priority A Step 5: Application visibly confirms selected Vault (button is tab-vault.ok and badge shows synthetic_vault)
    const vaultBtn = page.locator("button.tab-vault.ok");
    await expect(vaultBtn).toBeVisible({ timeout: 15000 });
    const badge = page.locator("button.tab-vault span.badge.ok");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveText("synthetic_vault");

    // Priority A Step 6: Enter novel observation distinct from all vault notes
    const composerTextarea = page.locator("#chat-composer-textarea");
    await composerTextarea.fill(
      "Observation: Urban bicyclists frequently navigate dedicated green corridors faster during evening commutes."
    );

    const submitBtn = page.locator("button.submit-btn");
    await submitBtn.click();

    // Priority A Step 7: Verify Executable Action banner (Green) and proposal review
    const execBanner = page.locator(".proposal-executability-banner.exec-create");
    await expect(execBanner).toBeVisible({ timeout: 15000 });
    await expect(execBanner).toContainText("Executable Action (CREATE)");
    await expect(page.locator(".proposal-title")).toContainText("Urban bicyclists", { timeout: 15000 });

    // Ensure run_id is resolved and populated before decision
    const runIdCode = page.locator(".run-id-label code");
    await expect(runIdCode).toBeVisible({ timeout: 15000 });
    const runId = (await runIdCode.textContent())?.trim() ?? "";
    expect(runId).toBeTruthy();

    // Approve the proposal
    const approveBtn = page.locator("button.btn.approve:has-text('Approve')");
    await expect(approveBtn).toBeVisible({ timeout: 15000 });
    await expect(approveBtn).toBeEnabled({ timeout: 15000 });
    await approveBtn.click();

    // Verify workflow transitions to completion
    const reviewStage = page.locator("ol.timeline li:has-text('Human Review')");
    await expect(reviewStage).toHaveAttribute("data-state", "completed", { timeout: 15000 });

    const execStage = page.locator("ol.timeline li:has-text('Controlled Execution')");
    await expect(execStage).toHaveAttribute("data-state", "completed", { timeout: 15000 });

    // Verify staged markdown note in staging directory
    const stagedFiles = fs.readdirSync(info.staging_dir).filter((f: string) => f.endsWith(".md"));
    expect(stagedFiles.length).toBeGreaterThanOrEqual(1);

    // Verify STRICT READ-ONLY PROTECTION on the synthetic Obsidian Vault:
    const postVaultState = readVaultState(info.vault_dir!);
    expect(postVaultState).toEqual(initialVaultState);
  });

  test("VAULT MODE: Duplicate detection against authentic Vault note -> DO_NOT_IMPORT & zero staging", async ({ page }) => {
    expect(serverInfo).not.toBeNull();
    const info = serverInfo!;

    // Suppress onboarding modals in browser state
    await page.addInitScript(() => {
      localStorage.setItem("lae-first-vault", "1");
      localStorage.setItem("lae-welcome-dismissed", "1");
    });

    await page.goto(info.url);
    await expect(page).toHaveTitle(/LivingAuthenticity/);
    await expect(page.locator("#chat-composer-textarea")).toBeVisible();

    // Ensure application is connected and in Vault Mode
    await expect(page.locator(".live-status-pill .live-dot.on")).toBeVisible({ timeout: 15000 });
    const vaultBtn = page.locator("button.tab-vault");
    if (!await vaultBtn.isVisible()) {
      const testBtn = page.locator("button.tab-test");
      if (await testBtn.isVisible()) {
        await testBtn.click();
      }
    }
    await expect(vaultBtn).toBeVisible({ timeout: 15000 });

    // Clear staging before this test
    const initialStagedCount = fs.readdirSync(info.staging_dir).filter((f: string) => f.endsWith(".md")).length;

    // Submit identical duplicate text matching duplicate-target.md in vault
    const composerTextarea = page.locator("#chat-composer-textarea");
    await composerTextarea.fill(
      "Exact identical overlap statement: researchers often experience fatigue after four hours of continuous writing."
    );

    const submitBtn = page.locator("button.submit-btn");
    await submitBtn.click();

    // Proposal card should show non-executable warning banner
    const warningBanner = page.locator(".proposal-executability-banner");
    await expect(warningBanner).toBeVisible({ timeout: 15000 });
    await expect(warningBanner).toContainText(/Non-Executable (Action|Proposal)/);

    // Acknowledge button should be present for DO_NOT_IMPORT
    const ackBtn = page.locator("button.btn.acknowledge");
    await expect(ackBtn).toBeVisible({ timeout: 15000 });
    await expect(ackBtn).toBeEnabled({ timeout: 15000 });
    await ackBtn.click();

    // Verify UI acknowledgment confirmation
    const ackConfirmation = page.locator(".ok-text:has-text('Acknowledged')");
    await expect(ackConfirmation).toBeVisible({ timeout: 15000 });

    // Review stage must transition to completed once human acknowledges
    const reviewStage = page.locator("ol.timeline li:has-text('Human Review')");
    await expect(reviewStage).toHaveAttribute("data-state", "completed", { timeout: 15000 });

    // Controlled execution must NOT be completed
    const execStage = page.locator("ol.timeline li:has-text('Controlled Execution')");
    const execState = await execStage.getAttribute("data-state");
    expect(execState).not.toBe("completed");

    // Zero new staging files written
    const finalStagedCount = fs.readdirSync(info.staging_dir).filter((f: string) => f.endsWith(".md")).length;
    expect(finalStagedCount).toBe(initialStagedCount);
  });
});
