/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    // Vitest 4.1.x on Node 24 can lose the AsyncLocalStorage "current
    // suite" context in the default fork/threads pools (a known tinypool
    // regression), surfacing as "Vitest failed to find the current suite"
    // from setup hooks — a collection-time failure unrelated to any test.
    // The vmForks pool runs each test file in an in-process VM context,
    // preserving that suite context; isolate:false shares the global
    // `expect` so @testing-library/jest-dom matchers still register.
    pool: "vmForks",
    isolate: false,
  },
});
