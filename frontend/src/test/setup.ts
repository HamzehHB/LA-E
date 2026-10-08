import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Ensure each test unmounts its render so queries never match stale DOM.
afterEach(() => {
  cleanup();
});
