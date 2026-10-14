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
});
