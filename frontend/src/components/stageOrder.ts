/**
 * Backend stage order: the REAL backend STAGE_ORDER from
 * orchestrator.py is preserved 1:1 in the domain contract.
 * The UI may GROUP stages visually; groups below reference backend keys.
 */
import type { BackendStage } from "../contracts/backend";

export const BACKEND_STAGE_ORDER: BackendStage[] = [
  "ingestion",
  "retrieval",
  "comparison",
  "relation",
  "core",
  "classification",
  "proposal",
  "confidence",
  "knowledge_filter",
  "representation",
];

export interface StageGroup {
  key: string;
  labelKey: string;
  stages: BackendStage[];
}

/** Presentation-only grouping; every entry maps to real backend stages. */
export const STAGE_GROUPS: StageGroup[] = [
  { key: "ingest", labelKey: "ingestion", stages: ["ingestion"] },
  { key: "evidence", labelKey: "retrieval", stages: ["retrieval", "comparison", "relation", "core"] },
  { key: "decide", labelKey: "classification", stages: ["classification", "proposal", "confidence"] },
  { key: "govern", labelKey: "knowledge_filter", stages: ["knowledge_filter", "representation"] },
];
