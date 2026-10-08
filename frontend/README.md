# Frontend (presentation-only foundation)

Status: static UI foundation with synthetic fixtures. No live backend wiring.
No launcher code, no HTTP/SSE implementation, no security-test changes.

Current scope: typed contracts, mock fixtures, safe presentation states.
Future integration scope: launcher, live transport, filesystem picker,
backend synchronization, real dependencies, embedding, vector indexing,
audit filesystem routing, safe-stop/resume, LLM integration (all NOT in the current foundation).

## Structure

- `src/contracts/backend.ts` — backend DTO mirrors (frozen Python dataclasses).
- `src/contracts/transport.ts` — proposed launcher wire format (not implemented).
- `src/contracts/ui.ts` — frontend-only view models + error/config states.
- `src/fixtures/demo.ts` — synthetic `<TEST_*>` fixtures only.
- `src/services/transport.ts` — single mock transport (no URLs in components).
- `src/state/store.tsx` — React context state (theme persisted locally only).
- `src/i18n/resources.ts` — `ui.*` namespace mirroring backend `app/staging/audit` discipline.
- `src/components/` — TopNav, Workflow/CurrentAudit, Panels (result/journals/composer/settings/onboarding/engine/safe-stop).
- `src/test/app.test.tsx` — behavior tests (no snapshots for coverage).
- `docs/launcher-contract.md` — non-executable API/SSE contract.

## Commands (run inside `frontend/`)

- `npm install`
- `npm run typecheck`
- `npm test`
- `npm run build`

## Backend pipeline mapping

Real `STAGE_ORDER` (10 stages) is preserved in `BACKEND_STAGE_ORDER`;
`STAGE_GROUPS` only groups them visually with explicit backend-key references.

## Notes

- Branding: project/documentation identity is "LivingAuthenticity Environment";
  the UI header displays "LA-E" by design (UI display name only).

- Proposal hashes in fixtures are static display strings (no authoritative hashing).
- Mock Approve/Reject/Edit mutate local fixture state only (`mock_*` prefix).
- Theme persists to `localStorage` (`lae-theme`); no secrets/paths persisted.
- Journals show max 10 fixture rows; rotation interval is cancellable/cleaned up.
