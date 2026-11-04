import { describe, expect, it, vi, afterEach } from "vitest";
import { createLiveClient } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).__LAE_BOOTSTRAP__;
});

describe("live client availability", () => {
  it("is unavailable without a bootstrap token (dev/preview)", () => {
    const client = createLiveClient();
    expect(client.available).toBe(false);
  });

  it("never exposes the token and sends it via header only", async () => {
    (window as unknown as Record<string, unknown>).__LAE_BOOTSTRAP__ = {
      token: "secret-token",
      instanceId: "inst-1",
      version: "0.1.0",
    };
    const seen: { url: string; headers: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        seen.push({ url, headers: init?.headers });
        return {
          ok: true,
          json: async () => ({ run_id: "run-1", accepted: true, reason: "" }),
        } as Response;
      }),
    );
    const client = createLiveClient();
    expect(client.available).toBe(true);
    // Token must not be serializable/leaked via the client surface.
    expect(JSON.stringify(client)).not.toContain("secret-token");
    await client.startRun({
      input_text: "hello",
      source_mode: "personal_vault",
      app_language: "en",
      staging_language: "en",
      audit_language: "en",
    });
    expect(seen).toHaveLength(1);
    expect(seen[0].url).not.toContain("secret-token");
    expect(
      (seen[0].headers as Record<string, string>)["X-LAE-Token"],
    ).toBe("secret-token");
  });

  it("enforces exact wire contract and mandatory run_id in approve", async () => {
    (window as unknown as Record<string, unknown>).__LAE_BOOTSTRAP__ = {
      token: "secret-token",
      instanceId: "inst-1",
      version: "0.1.0",
    };
    const seen: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        seen.push({ url, body: JSON.parse(init?.body as string) });
        return {
          ok: true,
          json: async () => ({ proposal_hash: "h-123", approved: true, reason: "explicit UI decision" }),
        } as Response;
      }),
    );
    const client = createLiveClient();
    const res = await client.approve({
      proposal_hash: "h-123",
      query_unit_id: "qu-1",
      decision: "approve",
      run_id: "run-abc",
      proposal_action: "CREATE",
    });
    expect(res.approved).toBe(true);
    expect(seen).toHaveLength(1);
    expect(seen[0].url).toBe("/api/approve");
    expect(seen[0].body).toEqual({
      proposal_hash: "h-123",
      query_unit_id: "qu-1",
      decision: "approve",
      run_id: "run-abc",
      proposal_action: "CREATE",
    });
  });

  it("enforces exact wire contract and mandatory run_id in acceptProposal", async () => {
    (window as unknown as Record<string, unknown>).__LAE_BOOTSTRAP__ = {
      token: "secret-token",
      instanceId: "inst-1",
      version: "0.1.0",
    };
    const seen: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        seen.push({ url, body: JSON.parse(init?.body as string) });
        return {
          ok: true,
          json: async () => ({ accepted: true, action: "NEEDS_REVIEW", run_id: "run-xyz", proposal_hash: "h-456" }),
        } as Response;
      }),
    );
    const client = createLiveClient();
    const res = await client.acceptProposal({
      proposal_hash: "h-456",
      run_id: "run-xyz",
      proposal_action: "NEEDS_REVIEW",
    });
    expect(res.accepted).toBe(true);
    expect(seen).toHaveLength(1);
    expect(seen[0].url).toBe("/api/run/accept");
    expect(seen[0].body).toEqual({
      proposal_hash: "h-456",
      run_id: "run-xyz",
      proposal_action: "NEEDS_REVIEW",
    });
  });

  it("propagates error messages on HTTP 400 or 409 failures", async () => {
    (window as unknown as Record<string, unknown>).__LAE_BOOTSTRAP__ = {
      token: "secret-token",
      instanceId: "inst-1",
      version: "0.1.0",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return {
          ok: false,
          status: 409,
          json: async () => ({ error: { kind: "conflict", message: "run_id does not match the pending review run" } }),
        } as Response;
      }),
    );
    const client = createLiveClient();
    await expect(
      client.acceptProposal({
        proposal_hash: "h-456",
        run_id: "wrong-run-id",
      }),
    ).rejects.toThrow("conflict");
  });

  it("supports resumed_from_run_id in startRun and fixPausedRun", async () => {
    (window as unknown as Record<string, unknown>).__LAE_BOOTSTRAP__ = {
      token: "secret-token",
      instanceId: "inst-1",
      version: "0.1.0",
    };
    const calls: { url: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, body: init?.body ? JSON.parse(init.body as string) : null });
        if (url === "/api/run") {
          return {
            ok: true,
            json: async () => ({
              run_id: "run-2",
              accepted: true,
              reason: "",
              resumed_from_run_id: "run-1",
            }),
          } as Response;
        }
        if (url === "/api/run/fix") {
          return {
            ok: true,
            json: async () => ({
              run_id: "run-3",
              accepted: true,
              reason: "",
              resumed_from_run_id: "run-2",
            }),
          } as Response;
        }
        if (url === "/api/run/stop") {
          return {
            ok: true,
            json: async () => ({
              stopped: true,
              run_id: "run-3",
            }),
          } as Response;
        }
        return { ok: true, json: async () => ({}) } as Response;
      }),
    );

    const client = createLiveClient();
    const runRes = await client.startRun({
      input_text: "Observation: valid text here.",
      source_mode: "examples",
      app_language: "en",
      staging_language: "en",
      audit_language: "en",
      resumed_from_run_id: "run-1",
    });
    expect(runRes.run_id).toBe("run-2");
    expect(runRes.resumed_from_run_id).toBe("run-1");
    expect(calls[0].body).toEqual({
      input_text: "Observation: valid text here.",
      source_mode: "examples",
      app_language: "en",
      staging_language: "en",
      audit_language: "en",
      resumed_from_run_id: "run-1",
    });

    const fixRes = await client.fixPausedRun({
      run_id: "run-2",
      input_text: "Observation: corrected text.",
    });
    expect(fixRes.run_id).toBe("run-3");
    expect(fixRes.resumed_from_run_id).toBe("run-2");
    expect(calls[1].body).toEqual({
      run_id: "run-2",
      input_text: "Observation: corrected text.",
    });

    const stopRes = await client.stopPausedRun("run-3");
    expect(stopRes.stopped).toBe(true);
    expect(stopRes.run_id).toBe("run-3");
    expect(calls[2].body).toEqual({
      run_id: "run-3",
    });
  });
});
