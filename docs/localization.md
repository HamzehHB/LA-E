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

## Journal and Lifecycle Narrative Translation

Translation is a derived, traceable presentation layer that never authorizes execution or mutates authoritative sources.

### Bilingual Lifecycle Narratives
The audit subsystem provides canonical bilingual narrative generation (`src.living_authenticity.knowledge.governance.audit.narrative::generate_lifecycle_narrative`) in English (`en`) and Persian (`fa`):
- Provides deterministic, human-readable descriptions of all lifecycle outcomes: proposal generation, review states, explicit human approval/rejection, controlled execution, emergency journal persistence, full artifact quarantine, and partial cleanup failures.
- Persian translations use bidirectional isolation (Unicode isolates) so that machine tokens (hashes, UUIDs, filesystem paths, numbers, and outcome keys) remain LTR without visual disruption.

### TranslationRecord Contract & Provenance Integrity
`build_narrative_translation_record` constructs immutable, traceable translation records:
- **Cryptographic Binding:** Binds `source_id`, `source_kind` (e.g. `audit_narrative`, `dynamic_text`), `source_language`, `target_language`, `source_hash`, `source_text`, `translated_text`, `translator`, and `lifecycle_state`.
- **Truthful Provenance on Fallback:** When a custom translator raises an exception or returns an empty/whitespace string, fallback to deterministic template localization is performed. The translator provenance MUST be recorded as `"template-fallback"`, NEVER as `"custom-translator"`.
- **Custom Notes Preservation:** Custom notes or appended lines in the source narrative are faithfully preserved across fallback translation paths.
- **Exact Hash Parity:** `source_hash` is computed strictly from the canonical source narrative text being translated.
- **Stale Detection:** `is_narrative_translation_stale(record, current_narrative)` verifies whether the recorded `source_hash` matches the current narrative's SHA-256 hash. If source text changes after translation was generated, the record is flagged as `stale`.
- **Ephemeral Presentation Marker:** On-demand UI translations explicitly return `persisted: False`. Live translations exist solely in memory for presentation and never claim durable audit persistence.

## UI implementation

The UI exposes independent controls backed by the launcher API:
1. **App Presentation Language:** Configured via the Settings gear popover (`⚙`), toggling between English and Persian. Updates `dir="rtl"` / `dir="ltr"` and `lang` on `document.documentElement` and persists to `Config/localization.local.yaml`.
2. **Knowledge Language:** Configured via the top-header `Knowledge Language` dropdown menu, with separate selections for `Staging Language` and `Audit Language`. Persists via `POST /api/config`.
3. **Live Translation Actions (`TranslateAction`):** Inline controls on proposals, notes, and journal excerpts send requests to `POST /api/translate`, querying the active LLM provider or using the deterministic offline bilingual mapping table.
   - For audit narratives, passes `source_kind="audit_narrative"` and `source_id=record_id`.
   - The UI evaluates `source_hash` against current active text and displays a `[stale]` indicator if the underlying content changed since translation was fetched.
Validation, resource parity, and translation lifecycle stay strictly in the backend.
