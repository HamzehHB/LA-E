/**
 * Live launcher transport client (Increment 1).
 * Token stays in memory: never localStorage/sessionStorage/URL/log.
 * Same-origin fetch with X-LAE-Token; SSE via fetch reader.
 * Without a bootstrap token (vite dev), reports unavailable and the
 * UI keeps fixture/demo behavior.
 */
import type {
  AcceptProposalResponse,
  ApproveRequest,
  ApproveResponse,
  CancelRequest,
  CancelResponse,
  ConsentRequest,
  ConsentResponse,
  EditProposalRequest,
  EditProposalResponse,
  FolderPickResponse,
  FolderPurpose,
  HealthResponse,
  JournalsResponse,
  RevealFolderResponse,
  RunEvent,
  RunRequest,
  RunResponse,
  StateResponse,
  TranslateRequest,
  TranslateResponse,
  VaultIndexResponse,
  VaultSyncDecideRequest,
  VaultSyncDecideResponse,
  VaultSyncStatusResponse,
} from "../contracts/transport";

export const TOKEN_HEADER = "X-LAE-Token";

interface Bootstrap {
  token?: string;
  instanceId?: string;
  version?: string;
}

function bootstrap(): Bootstrap | null {
  try {
    const w = window as unknown as { __LAE_BOOTSTRAP__?: Bootstrap };
    const b = w.__LAE_BOOTSTRAP__;
    if (b && typeof b.token === "string" && b.token.length > 0) return b;
  } catch {
    /* ignore */
  }
  return null;
}

export interface LiveClient {
  available: boolean;
  instanceId: string;
  version: string;
  health(): Promise<HealthResponse>;
  state(): Promise<StateResponse>;
  updateConfig(updates: Record<string, unknown>): Promise<{ success: boolean; modes: unknown }>;
  pickFolder(purpose: FolderPurpose): Promise<FolderPickResponse>;
  revealFolder(purpose: FolderPurpose): Promise<RevealFolderResponse>;
  indexVault(): Promise<VaultIndexResponse>;
  getVaultSyncStatus(): Promise<VaultSyncStatusResponse>;
  authorizeVaultSync(vault_root?: string): Promise<VaultIndexResponse>;
  revokeVaultSync(): Promise<{ authorized: boolean; status: string }>;
  reconcileVaultSync(): Promise<unknown>;
  decideVaultSync(req: VaultSyncDecideRequest): Promise<VaultSyncDecideResponse>;
  startRun(req: RunRequest): Promise<RunResponse>;
  approve(req: ApproveRequest): Promise<ApproveResponse>;
  acceptProposal(run_id?: string): Promise<AcceptProposalResponse>;
  editProposal(req: EditProposalRequest): Promise<EditProposalResponse>;
  consent(req: ConsentRequest): Promise<ConsentResponse>;
  cancel(req: CancelRequest): Promise<CancelResponse>;
  fixPausedRun(req: { run_id?: string; input_text?: string; source_mode?: string; retrieval_mode?: string }): Promise<RunResponse>;
  stopPausedRun(run_id?: string): Promise<{ stopped: boolean; run_id: string }>;
  translate(req: TranslateRequest): Promise<TranslateResponse>;
  journals(): Promise<JournalsResponse>;
  streamEvents(
    onEvent: (event: RunEvent) => void,
    onError: (message: string) => void,
  ): () => void;
}

class FetchLiveClient implements LiveClient {
  readonly available = true;
  readonly instanceId: string;
  readonly version: string;
  private streamSeq = 0;

  constructor(
    token: string,
    instanceId: string,
    version: string,
  ) {
    this.instanceId = instanceId;
    this.version = version;
    // Token lives in closure scope only: attached via non-enumerable
    // properties so JSON.stringify / spread / enumeration cannot leak it.
    const headers = (): Record<string, string> => ({
      [TOKEN_HEADER]: token,
      "Content-Type": "application/json",
    });
    const authHeader = (): Record<string, string> => ({
      [TOKEN_HEADER]: token,
    });
    Object.defineProperties(this, {
      headers: { value: headers, enumerable: false, writable: false },
      authHeader: { value: authHeader, enumerable: false, writable: false },
    });
  }

  private readonly headers!: () => Record<string, string>;
  private readonly authHeader!: () => Record<string, string>;

  private async post<T>(path: string, body: unknown): Promise<T> {
    let resp: Response;
    try {
      resp = await fetch(path, {
        method: "POST",
        headers: this.headers(),
        body: JSON.stringify(body),
      });
    } catch {
      throw new Error("connection_unavailable");
    }
    if (!resp.ok) throw new Error(await this.errorKind(resp));
    return (await resp.json()) as T;
  }

  private async get<T>(path: string): Promise<T> {
    let resp: Response;
    try {
      resp = await fetch(path, {
        headers: this.authHeader(),
      });
    } catch {
      throw new Error("connection_unavailable");
    }
    if (!resp.ok) throw new Error(await this.errorKind(resp));
    return (await resp.json()) as T;
  }

  private async errorKind(resp: Response): Promise<string> {
    try {
      const payload = (await resp.json()) as {
        error?: { kind?: string; message?: string };
      };
      const kind = payload?.error?.kind;
      if (typeof kind === "string" && kind.length > 0) return kind;
    } catch {
      /* fall through */
    }
    if (resp.status === 401 || resp.status === 403) {
      return "authentication_error";
    }
    if (resp.status === 413) return "validation_error";
    return "backend_error";
  }

  async health(): Promise<HealthResponse> {
    const resp = await fetch("/api/health");
    if (!resp.ok) throw new Error("connection_unavailable");
    return (await resp.json()) as HealthResponse;
  }

  async state(): Promise<StateResponse> {
    let resp: Response;
    try {
      resp = await fetch("/api/state", {
        headers: this.authHeader(),
      });
    } catch {
      throw new Error("connection_unavailable");
    }
    if (!resp.ok) throw new Error(await this.errorKind(resp));
    return (await resp.json()) as StateResponse;
  }

  updateConfig(updates: Record<string, unknown>): Promise<{ success: boolean; modes: unknown }> {
    return this.post<{ success: boolean; modes: unknown }>("/api/config", updates);
  }

  pickFolder(purpose: FolderPurpose): Promise<FolderPickResponse> {
    return this.post<FolderPickResponse>("/api/folders/pick", { purpose });
  }

  revealFolder(purpose: FolderPurpose): Promise<RevealFolderResponse> {
    return this.post<RevealFolderResponse>("/api/folders/reveal", { purpose });
  }

  indexVault(): Promise<VaultIndexResponse> {
    return this.post<VaultIndexResponse>("/api/vault/index", {});
  }

  getVaultSyncStatus(): Promise<VaultSyncStatusResponse> {
    return this.get<VaultSyncStatusResponse>("/api/vault/sync/status");
  }

  authorizeVaultSync(vault_root?: string): Promise<VaultIndexResponse> {
    return this.post<VaultIndexResponse>("/api/vault/sync/authorize", { vault_root });
  }

  revokeVaultSync(): Promise<{ authorized: boolean; status: string }> {
    return this.post<{ authorized: boolean; status: string }>("/api/vault/sync/revoke", {});
  }

  reconcileVaultSync(): Promise<unknown> {
    return this.post<unknown>("/api/vault/sync/reconcile", {});
  }

  decideVaultSync(req: VaultSyncDecideRequest): Promise<VaultSyncDecideResponse> {
    return this.post<VaultSyncDecideResponse>("/api/vault/sync/decide", req);
  }

  startRun(req: RunRequest): Promise<RunResponse> {
    return this.post<RunResponse>("/api/run", req);
  }

  approve(req: ApproveRequest): Promise<ApproveResponse> {
    return this.post<ApproveResponse>("/api/approve", req);
  }

  acceptProposal(run_id?: string): Promise<AcceptProposalResponse> {
    return this.post<AcceptProposalResponse>("/api/run/accept", { run_id });
  }

  editProposal(req: EditProposalRequest): Promise<EditProposalResponse> {
    return this.post<EditProposalResponse>("/api/proposal/edit", req);
  }

  consent(req: ConsentRequest): Promise<ConsentResponse> {
    return this.post<ConsentResponse>("/api/consent", req);
  }

  cancel(req: CancelRequest): Promise<CancelResponse> {
    return this.post<CancelResponse>("/api/cancel", req);
  }

  fixPausedRun(req: { run_id?: string; input_text?: string; source_mode?: string; retrieval_mode?: string }): Promise<RunResponse> {
    return this.post<RunResponse>("/api/run/fix", req);
  }

  stopPausedRun(run_id?: string): Promise<{ stopped: boolean; run_id: string }> {
    return this.post<{ stopped: boolean; run_id: string }>("/api/run/stop", { run_id });
  }

  translate(req: TranslateRequest): Promise<TranslateResponse> {
    return this.post<TranslateResponse>("/api/translate", req);
  }

  async journals(): Promise<JournalsResponse> {
    let resp: Response;
    try {
      resp = await fetch("/api/journals", {
        headers: this.authHeader(),
      });
    } catch {
      throw new Error("connection_unavailable");
    }
    if (!resp.ok) throw new Error(await this.errorKind(resp));
    return (await resp.json()) as JournalsResponse;
  }

  streamEvents(
    onEvent: (event: RunEvent) => void,
    onError: (message: string) => void,
  ): () => void {
    const ctrl = new AbortController();
    let stopped = false;
    const client = this;
    void (async () => {
      try {
        const resp = await fetch(`/api/events?after=${client.streamSeq}`, {
          headers: {
            ...client.authHeader(),
            Accept: "text/event-stream",
          },
          signal: ctrl.signal,
        });
        if (!resp.ok || !resp.body) {
          onError("connection_unavailable");
          return;
        }
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { done, value } = await reader.read();
          if (done || stopped) break;
          buffer += decoder.decode(value, { stream: true });
          let idx: number;
          while ((idx = buffer.indexOf("\n\n")) >= 0) {
            const frame = buffer.slice(0, idx);
            buffer = buffer.slice(idx + 2);
            const line = frame.split("\n").find((l) => l.startsWith("data:"));
            if (!line) continue;
            try {
              const event = JSON.parse(line.slice(5).trim()) as RunEvent & {
                seq?: number;
              };
              if (typeof event.seq === "number") client.streamSeq = event.seq;
              onEvent(event);
            } catch {
              /* ignore malformed frame */
            }
          }
        }
      } catch (e) {
        if (!stopped && (e as Error)?.name !== "AbortError") {
          onError("connection_unavailable");
        }
      }
    })();
    return () => {
      stopped = true;
      ctrl.abort();
    };
  }
}

const unavailable: LiveClient = {
  available: false,
  instanceId: "",
  version: "",
  health: () => Promise.reject(new Error("connection_unavailable")),
  state: () => Promise.reject(new Error("connection_unavailable")),
  updateConfig: () => Promise.reject(new Error("connection_unavailable")),
  pickFolder: () => Promise.reject(new Error("connection_unavailable")),
  revealFolder: () => Promise.reject(new Error("connection_unavailable")),
  indexVault: () => Promise.reject(new Error("connection_unavailable")),
  getVaultSyncStatus: () => Promise.reject(new Error("connection_unavailable")),
  authorizeVaultSync: () => Promise.reject(new Error("connection_unavailable")),
  revokeVaultSync: () => Promise.reject(new Error("connection_unavailable")),
  reconcileVaultSync: () => Promise.reject(new Error("connection_unavailable")),
  decideVaultSync: () => Promise.reject(new Error("connection_unavailable")),
  startRun: () => Promise.reject(new Error("connection_unavailable")),
  approve: () => Promise.reject(new Error("connection_unavailable")),
  acceptProposal: () => Promise.reject(new Error("connection_unavailable")),
  editProposal: () => Promise.reject(new Error("connection_unavailable")),
  consent: () => Promise.reject(new Error("connection_unavailable")),
  cancel: () => Promise.reject(new Error("connection_unavailable")),
  fixPausedRun: () => Promise.reject(new Error("connection_unavailable")),
  stopPausedRun: () => Promise.reject(new Error("connection_unavailable")),
  translate: () => Promise.reject(new Error("connection_unavailable")),
  journals: () => Promise.reject(new Error("connection_unavailable")),
  streamEvents: (_onEvent, onError) => {
    queueMicrotask(() => onError("connection_unavailable"));
    return () => undefined;
  },
};

/** Build the live client once; token never leaves module memory. */
export function createLiveClient(): LiveClient {
  const b = bootstrap();
  if (!b?.token) return unavailable;
  return new FetchLiveClient(b.token, b.instanceId ?? "", b.version ?? "");
}
