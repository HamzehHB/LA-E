# Frontend (LA-E v0.1 UI)

Status: fully integrated with the local loopback launcher (`src/living_authenticity/adapter/`) while maintaining standalone Test Mode with synthetic fixtures when running unattached.

## Architecture

- **Loopback Launcher Integration:** When launched via `python -m src.living_authenticity.main --launcher` (or `python -m src.living_authenticity.adapter`), the backend injects an ephemeral session token via `window.__LAE_BOOTSTRAP__`. `src/live/client.ts` handles typed REST API requests and the Server-Sent Events (`/api/events`) stream over loopback `127.0.0.1`.
- **Test Mode vs. Vault Mode:**
  - **`TEST` Mode:** Completely isolated. Uses synthetic fixtures (`examples/bounded_local_integration/corpus` and `examples/bounded_local_integration/core`), never touches the personal vault, and presents safe mock review affordances.
  - **`VAULT` Mode:** Connects to the user's authoritative vault directory. Initial vector indexing is performed into an isolated vector database directory using BGE-M3 and LanceDB.
- **Workflow & Current Audit:**
  - 17-node chronological timeline with real backend status tracking and color states.
  - Upper constellation band with 13 connected stars and DeepThink `Current Audit` live stream overlay active only during runs.
- **Processing Result & Governance:**
  - Numeric confidence score (`0.00`–`1.00`) and score band (`high`, `moderate`, `low`).
  - Strict human-gated approval (`Approve`, `Edit proposal`, `Reject`).
  - Pause / Fix / Stop error recovery for halted runs.
  - Bilingual translation (English ↔ Persian) backed by backend LLM with deterministic offline fallback.
- **Stacked Journals:**
  - Upper: `Summary Of Recent Audits` (passed and failed records; `errors/` directory strictly excluded).
  - Lower: `Summary Of Recent Staging` (1:1 paired with audit records, indicating staging availability).
  - 5-second synchronized rotation, manual paging (‹ ›), and "View All" historical table modal.

## Structure

- `src/contracts/backend.ts` — backend DTO mirrors (frozen Python dataclasses).
- `src/contracts/transport.ts` — launcher wire format and DTO contracts.
- `src/contracts/ui.ts` — view models, error states, and config models.
- `src/fixtures/demo.ts` — synthetic `<TEST_*>` fixtures for standalone test mode.
- `src/live/client.ts` — typed SSE and REST client communicating with loopback launcher.
- `src/state/store.tsx` — React context state, live client synchronization, and session management.
- `src/i18n/resources.ts` — comprehensive English and Persian (`fa`) translations.
- `src/components/` — `TopNav`, `Workflow`, `Panels` (ProcessingResult, Journals, Composer, Welcome, etc.).
- `src/test/app.test.tsx` — complete Vitest component test suite.
- `docs/launcher-contract.md` — executable API and SSE wire specifications.

## Commands (run inside `frontend/`)

- `npm install`
- `npm run typecheck`
- `npm test`
- `npm run build`
