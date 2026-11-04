declare module "child_process" {
  export interface ChildProcess {
    pid?: number;
    stdout: {
      on(event: "data", listener: (chunk: { toString(enc?: string): string }) => void): void;
    } | null;
    stderr: {
      on(event: "data", listener: (chunk: { toString(enc?: string): string }) => void): void;
    } | null;
    stdin: {
      end(): void;
    } | null;
    exitCode: number | null;
    signalCode: string | null;
    killed: boolean;
    on(event: "exit", listener: (code: number | null, signal: string | null) => void): void;
    on(event: "error", listener: (err: Error) => void): void;
    on(event: string, listener: (...args: any[]) => void): void;
    once(event: "exit", listener: (code: number | null, signal: string | null) => void): void;
    once(event: "error", listener: (err: Error) => void): void;
    once(event: string, listener: (...args: any[]) => void): void;
    removeListener(event: "exit", listener: (code: number | null, signal: string | null) => void): void;
    removeListener(event: "error", listener: (err: Error) => void): void;
    removeListener(event: string, listener: (...args: any[]) => void): void;
    kill(signal?: string): boolean;
  }

  export interface SpawnOptions {
    cwd?: string;
    stdio?: ("pipe" | "ignore" | "inherit")[];
    env?: Record<string, string | undefined>;
  }

  export function spawn(command: string, args?: readonly string[], options?: SpawnOptions): ChildProcess;
}

declare module "fs" {
  export function mkdirSync(path: string, options?: { recursive?: boolean }): string | undefined;
  export function readdirSync(path: string): string[];
  export function readFileSync(path: string, encoding: "utf-8" | string): string;
  export function statSync(path: string): { isDirectory(): boolean; isFile(): boolean };
  export function writeFileSync(path: string, data: string, encoding?: string): void;
  export function existsSync(path: string): boolean;
  export function rmSync(path: string, options?: { recursive?: boolean; force?: boolean }): void;
  export function realpathSync(path: string): string;
  export function symlinkSync(target: string, path: string, type?: "dir" | "file" | "junction"): void;
}

declare module "path" {
  export function resolve(...paths: string[]): string;
  export function join(...paths: string[]): string;
  export function relative(from: string, to: string): string;
  export function isAbsolute(path: string): boolean;
}

declare module "url" {
  export function fileURLToPath(url: string | URL): string;
}

declare module "os" {
  export function tmpdir(): string;
}

declare const process: {
  execPath: string;
  env: Record<string, string | undefined>;
};
