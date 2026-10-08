/**
 * Single configured transport abstraction.
 * Foundation build: mock transport backed by static fixtures. No URLs, no fetch,
 * no sockets in components. The eventual launcher origin/port/token
 * will be configured here only.
 */
import { FIXTURE_AUDITS, FIXTURE_STAGING, FIXTURE_RUN } from "../fixtures/demo";
import type { JournalsResponse } from "../contracts/transport";
import type { CurrentRunView, JournalRow } from "../contracts/ui";

export interface BackendTransport {
  readonly kind: "mock-fixture";
  getCurrentRun(): CurrentRunView;
  getJournals(): JournalsResponse;
  getAuditRows(): JournalRow[];
  getStagingRows(): JournalRow[];
}

class MockFixtureTransport implements BackendTransport {
  readonly kind = "mock-fixture" as const;
  getCurrentRun(): CurrentRunView {
    return { ...FIXTURE_RUN };
  }
  getJournals(): JournalsResponse {
    return { audits: [], staging: [] };
  }
  getAuditRows(): JournalRow[] {
    return [...FIXTURE_AUDITS];
  }
  getStagingRows(): JournalRow[] {
    return [...FIXTURE_STAGING];
  }
}

/** Single configured transport instance for the whole app. */
export const transport: BackendTransport = new MockFixtureTransport();
