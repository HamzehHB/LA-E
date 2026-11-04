/// <reference path="./node.d.ts" />
import * as child_process from "child_process";
import * as fs from "fs";
import * as path from "path";
import * as url from "url";

const __dirname = url.fileURLToPath(new URL(".", import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, "../..");
export const AUTHORIZED_TEST_OUTPUT_ROOT = path.resolve(WORKSPACE_ROOT, "tests/output");

/**
 * Returns true if child process has exited (either normal exitCode or signal termination).
 */
export function isProcessExited(proc: child_process.ChildProcess): boolean {
  return proc.exitCode !== null || proc.signalCode !== null;
}

/**
 * Await child process exit with bounded timeout and race-free listener handling.
 * Returns true if the process is confirmed exited, false if timed out.
 */
export function waitForExit(
  proc: child_process.ChildProcess,
  timeoutMs: number
): Promise<boolean> {
  // 1. Immediate exit check: if already exited, do not wait or set timers
  if (isProcessExited(proc)) {
    return Promise.resolve(true);
  }

  return new Promise<boolean>((resolve) => {
    let timer: any = null;

    const cleanup = () => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      proc.removeListener("exit", onExit);
      proc.removeListener("error", onError);
    };

    const onExit = () => {
      cleanup();
      resolve(true);
    };

    const onError = () => {
      if (isProcessExited(proc)) {
        cleanup();
        resolve(true);
      }
    };

    // Register exit and error listeners first to prevent missing events or unhandled errors
    proc.once("exit", onExit);
    proc.once("error", onError);

    // 2. Race elimination: re-check in case process exited between initial check and listener registration
    if (isProcessExited(proc)) {
      cleanup();
      resolve(true);
      return;
    }

    timer = setTimeout(() => {
      cleanup();
      // Final confirmation check: verify exit status at timeout boundary
      resolve(isProcessExited(proc));
    }, timeoutMs);
  });
}

/**
 * Terminate a child process with strict exit guarantees:
 * - Immediate return if already exited (does not rely on proc.killed).
 * - Graceful shutdown attempted first via stdin closure.
 * - Forced termination (SIGKILL) used only as a bounded fallback.
 * - Verifies confirmed process termination (isProcessExited) before returning.
 * - Throws an explicit Error if termination cannot be established.
 */
export async function terminateProcess(
  proc: child_process.ChildProcess | null,
  gracefulTimeoutMs = 5000,
  forceTimeoutMs = 3000
): Promise<void> {
  if (!proc) {
    return;
  }

  // 1. Already exited: return immediately without waiting
  if (isProcessExited(proc)) {
    return;
  }

  // 2. Attempt graceful shutdown first by closing stdin
  try {
    proc.stdin?.end();
  } catch {
    /* ignore stdin closure error if stdin was already closed */
  }

  // 3. Await graceful exit with bounded timeout
  const exitedGracefully = await waitForExit(proc, gracefulTimeoutMs);
  if (exitedGracefully || isProcessExited(proc)) {
    return;
  }

  // 4. Fallback: forced termination only if graceful shutdown did not exit
  try {
    proc.kill("SIGKILL");
  } catch {
    /* ignore kill error if process terminated concurrently */
  }

  // 5. Await forced termination with bounded timeout
  const exitedForced = await waitForExit(proc, forceTimeoutMs);
  if (!exitedForced && !isProcessExited(proc)) {
    throw new Error(
      "Process termination failed: child process remained active after graceful shutdown and SIGKILL fallback"
    );
  }
}

/**
 * Safely remove temporary test directories with bounded retry for Windows file locks.
 * Strictly verifies that the directory resides within the authorized test output boundary
 * (tests/output) inside the workspace root before performing deletion.
 *
 * Security & Reparse-Point Safeguards:
 * 1. Lexical boundary check: ensures resolvedTarget is strictly a child of authorizedBase.
 * 2. Canonical realpath check: resolves symlinks and Windows NTFS junctions via fs.realpathSync.
 *    Rejects any path that escapes the authorized base directory via reparse points.
 * 3. Base preservation: refuses deletion of the authorized base directory itself.
 *
 * Surfaces cleanup failures explicitly by throwing if directory cannot be removed.
 */
export async function removeDirSafely(
  dirPath: string,
  maxRetries = 6,
  retryDelayMs = 500,
  authorizedBase = AUTHORIZED_TEST_OUTPUT_ROOT
): Promise<void> {
  if (!dirPath) {
    return;
  }

  const resolvedTarget = path.resolve(dirPath);
  const resolvedBase = path.resolve(authorizedBase);

  // 1. Lexical boundary check: ensure resolvedTarget is strictly a descendant of resolvedBase
  const rel = path.relative(resolvedBase, resolvedTarget);
  if (rel.startsWith("..") || path.isAbsolute(rel) || rel === "") {
    throw new Error(
      `Security violation: attempt to remove directory '${resolvedTarget}' outside authorized test output directory '${resolvedBase}'`
    );
  }

  if (!fs.existsSync(resolvedTarget)) {
    return;
  }

  // 2. Canonical boundary check: resolve symlinks/junctions to guarantee real target is within authorizedBase
  const canonicalBase = fs.existsSync(resolvedBase) ? fs.realpathSync(resolvedBase) : resolvedBase;
  const canonicalTarget = fs.realpathSync(resolvedTarget);
  const realRel = path.relative(canonicalBase, canonicalTarget);
  if (realRel.startsWith("..") || path.isAbsolute(realRel) || realRel === "") {
    throw new Error(
      `Security violation: resolved path '${canonicalTarget}' via symlink/junction escapes authorized test output directory '${canonicalBase}'`
    );
  }

  let lastError: Error | null = null;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    try {
      if (fs.existsSync(resolvedTarget)) {
        fs.rmSync(resolvedTarget, { recursive: true, force: true });
      }
      return;
    } catch (err) {
      lastError = err instanceof Error ? err : new Error(String(err));
      await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
    }
  }

  if (lastError && fs.existsSync(resolvedTarget)) {
    throw new Error(
      `Failed to remove test directory ${resolvedTarget} after confirmed process termination: ${lastError.message}`
    );
  }
}
