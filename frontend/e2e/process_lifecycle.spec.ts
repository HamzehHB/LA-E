/// <reference path="./node.d.ts" />
import { test, expect } from "@playwright/test";
import * as child_process from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as url from "url";
import {
  isProcessExited,
  waitForExit,
  terminateProcess,
  removeDirSafely,
} from "./process_lifecycle";

const __dirname = url.fileURLToPath(new URL(".", import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "../..");

test.describe("Process Lifecycle & Termination Guarantees", () => {
  test("already-exited child process is detected immediately without waiting", async () => {
    // Spawn a quick-exit node command
    const proc = child_process.spawn(process.execPath, ["-e", "process.exit(0)"], {
      cwd: WORKSPACE_ROOT,
      stdio: ["ignore", "ignore", "ignore"],
    });

    // Wait until process exits
    const exited = await waitForExit(proc, 2000);
    expect(exited).toBe(true);
    expect(isProcessExited(proc)).toBe(true);
    expect(proc.exitCode).toBe(0);

    // Call terminateProcess on already-exited child: must complete immediately
    const start = Date.now();
    await terminateProcess(proc, 5000, 3000);
    const elapsed = Date.now() - start;

    expect(elapsed).toBeLessThan(100);
    expect(isProcessExited(proc)).toBe(true);
  });

  test("graceful termination closes stdin and awaits exit confirmation", async () => {
    // Child script that stays alive reading stdin and exits 0 on EOF
    const script = "process.stdin.on('end', () => process.exit(0)); process.stdin.resume();";
    const proc = child_process.spawn(process.execPath, ["-e", script], {
      cwd: WORKSPACE_ROOT,
      stdio: ["pipe", "ignore", "ignore"],
    });

    expect(isProcessExited(proc)).toBe(false);

    // Terminate via graceful shutdown
    await terminateProcess(proc, 2000, 1000);

    // Must be confirmed terminated with exitCode 0
    expect(isProcessExited(proc)).toBe(true);
    expect(proc.exitCode).toBe(0);
  });

  test("forced termination fallback executes when graceful shutdown is ignored", async () => {
    // Child script that ignores stdin end and loops indefinitely
    const script = "process.stdin.on('end', () => {}); setInterval(() => {}, 10000);";
    const proc = child_process.spawn(process.execPath, ["-e", script], {
      cwd: WORKSPACE_ROOT,
      stdio: ["pipe", "ignore", "ignore"],
    });

    expect(isProcessExited(proc)).toBe(false);

    // Terminate with short graceful timeout (200ms) to trigger forced SIGKILL fallback
    await terminateProcess(proc, 200, 2000);

    // Must be confirmed terminated (isProcessExited is true)
    expect(isProcessExited(proc)).toBe(true);
  });

  test("removeDirSafely cleans temporary directory inside workspace test output", async () => {
    const testDir = path.resolve(
      WORKSPACE_ROOT,
      "tests/output/playwright_lifecycle",
      `.lifecycle_test_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
    );
    fs.mkdirSync(testDir, { recursive: true });
    fs.writeFileSync(path.join(testDir, "test.txt"), "sample data", "utf-8");

    expect(fs.existsSync(testDir)).toBe(true);

    await removeDirSafely(testDir);

    expect(fs.existsSync(testDir)).toBe(false);
  });

  test("removeDirSafely rejects directory removal outside authorized test output directory", async () => {
    const unauthorizedOutsideDir = path.resolve(WORKSPACE_ROOT, "unauthorized_test_dir");

    await expect(
      removeDirSafely(unauthorizedOutsideDir)
    ).rejects.toThrow("Security violation: attempt to remove directory");
  });

  test("removeDirSafely rejects junction or symlink escaping authorized test output boundary", async () => {
    const authorizedSubdir = path.resolve(
      WORKSPACE_ROOT,
      "tests/output/playwright_lifecycle",
      `sandbox_scope_${Date.now()}`
    );
    const outsideTarget = path.resolve(
      WORKSPACE_ROOT,
      "tests/output/playwright_lifecycle",
      `target_outside_scope_${Date.now()}`
    );
    const junctionLink = path.resolve(authorizedSubdir, "link_to_outside");

    fs.mkdirSync(authorizedSubdir, { recursive: true });
    fs.mkdirSync(outsideTarget, { recursive: true });

    let junctionCreated = false;
    try {
      fs.symlinkSync(outsideTarget, junctionLink, "junction");
      junctionCreated = true;
    } catch {
      // If OS environment does not permit reparse point creation, test skips safely
    }

    if (junctionCreated) {
      try {
        await expect(
          removeDirSafely(junctionLink, 1, 10, authorizedSubdir)
        ).rejects.toThrow("Security violation: resolved path");

        expect(fs.existsSync(outsideTarget)).toBe(true);
      } finally {
        try {
          fs.rmSync(junctionLink, { force: true });
        } catch {
          /* ignore */
        }
      }
    }

    try {
      fs.rmSync(authorizedSubdir, { recursive: true, force: true });
      fs.rmSync(outsideTarget, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  });

  test("retains temporary directory when process termination cannot be confirmed", async () => {
    const testDir = path.resolve(
      WORKSPACE_ROOT,
      "tests/output/playwright_lifecycle",
      `.lifecycle_retain_test_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
    );
    fs.mkdirSync(testDir, { recursive: true });
    fs.writeFileSync(path.join(testDir, "state.txt"), "active state", "utf-8");

    // Simulate an unconfirmed/un-exited process by creating a mock child process where exit status is unconfirmed
    const mockUnterminatedProc: any = {
      exitCode: null,
      signalCode: null,
      killed: false,
      stdin: { end() {} },
      once(ev: string, cb: () => void) {},
      removeListener(ev: string, cb: () => void) {},
      kill() {
        return false;
      },
    };

    let cleanupErrorOccurred = false;
    let dirRemoved = false;

    try {
      // terminateProcess must throw because process termination cannot be established
      await terminateProcess(mockUnterminatedProc, 100, 100);
    } catch {
      cleanupErrorOccurred = true;
    }

    // Since termination failed, directory removal must NOT be performed
    if (isProcessExited(mockUnterminatedProc)) {
      await removeDirSafely(testDir);
      dirRemoved = true;
    }

    expect(cleanupErrorOccurred).toBe(true);
    expect(dirRemoved).toBe(false);
    expect(fs.existsSync(testDir)).toBe(true);

    // Clean up testDir after assertion
    fs.rmSync(testDir, { recursive: true, force: true });
  });

  test("spawn error is detected and allows safe cleanup", async () => {
    const testDir = path.resolve(
      WORKSPACE_ROOT,
      "tests/output/playwright_lifecycle",
      `.lifecycle_spawn_err_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`
    );
    fs.mkdirSync(testDir, { recursive: true });

    // Spawn an invalid executable
    const proc = child_process.spawn("non_existent_binary_for_test_xyz_123", []);

    const spawnErrPromise = new Promise<Error>((resolve) => {
      proc.on("error", (err: Error) => resolve(err));
    });

    const spawnErr = await spawnErrPromise;
    expect(spawnErr).toBeDefined();

    // Since spawn failed, process exit status is established
    expect(isProcessExited(proc)).toBe(true);

    // terminateProcess should return immediately without throwing
    await terminateProcess(proc, 100, 100);

    // Directory cleanup proceeds cleanly
    await removeDirSafely(testDir);
    expect(fs.existsSync(testDir)).toBe(false);
  });
});
