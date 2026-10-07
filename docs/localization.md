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

## UI contract

The future UI exposes three independent controls (Application / Staging
Journal / Audit Journal language), each updating only its own domain.
Validation, resource parity, and translation lifecycle stay in the backend.
