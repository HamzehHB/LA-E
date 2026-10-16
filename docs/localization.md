# Localization Architecture

Three INDEPENDENT language domains (English `en` / Persian `fa` only):

* `app` (APP / UI / PROCESS presentation language)
* `staging` (STAGING NOTE output language for newly generated content)
* `audit` (AUDIT human-readable presentation language)

Changing one domain never changes another. Knowledge language follows the
source knowledge itself unless `staging` is explicitly configured
otherwise; app/audit languages never translate source knowledge.

## Configuration

Committed defaults live in `Config/localization.example.yaml` (all `en`).
Optional gitignored `Config/localization.local.yaml` overrides each domain
independently. `Config/settings.py::load_languages` validates values and
rejects anything outside `en`/`fa`. The same three settings are exposed
as `--app-language` / `--staging-language` / `--audit-language` CLI flags
for the future UI (three separate controls, one per journal/domain).

## Resources

Static project text lives in
`src/living_authenticity/localization/resources/en.json` and `fa.json`
with identical keys namespaced `app.*` / `staging.*` / `audit.*`.
Dynamic knowledge, evidence, provenance, paths, hashes, IDs, and machine
fields never go into these resources. `validate_resources()` enforces key
parity, placeholder parity, non-empty values, and domain coverage; missing
translations render with an explicit `[missing:{lang}:{key}]` marker rather
than silent fallback.

## RTL strategy

Persian prose stays RTL; machine-like values (paths, hashes, UUIDs, IDs,
filenames, numbers, code identifiers, timestamps, URLs) keep their LTR
shape wrapped in Unicode isolates (LRI...PDI for values, RLI...PDI for
prose). Nothing reverses or reorders characters. See
`src/living_authenticity/localization/bidi_core.py`.

## Journal translation

Translation is a derived, traceable presentation layer. `TranslationRecord`
binds every representation to source identity + source/target language +
source hash + translator identity, with `current` / `stale` / `missing` /
`invalid` / `unavailable` states. Sources are never overwritten; machine
fields are copied verbatim; translation never authorizes execution and
never creates a new proposal. Failures return UNAVAILABLE records that
preserve the source text.

## UI implementation

The UI exposes independent controls backed by the launcher API:
1. **App Presentation Language:** Configured via the Settings gear popover (`⚙`), toggling between English and Persian. Updates `dir="rtl"` / `dir="ltr"` and `lang` on `document.documentElement` and persists to `Config/localization.local.yaml`.
2. **Knowledge Language:** Configured via the top-header `Knowledge Language` dropdown menu, with separate selections for `Staging Language` and `Audit Language`. Persists via `POST /api/config`.
3. **Live Translation Actions:** Inline `TranslateAction` controls on proposals, notes, and journal excerpts send requests to `POST /api/translate`, querying the active LLM provider or using the deterministic offline bilingual mapping table.
Validation, resource parity, and translation lifecycle stay strictly in the backend.
