# LivingAuthenticity Environment — Development Setup

This document describes the local development environment and the basic requirements for running the **LivingAuthenticity Environment**.

## Python

* Python 3.13+

## External Software

The following software should be installed before running the project:

* Git
* Visual Studio Code
* Ollama
* Obsidian
* Zotero

## Models

### Embedding Model

* BGE-M3

### Local LLM

* Meta-Llama-3.1-8B-Instruct-Q5_K_M.gguf

Models are not downloaded automatically by the project. They must already exist in the configured model directories.

---

## Project Layout

The project separates **source code** from **persistent data**.

### Source Code

The repository contains:

* Source code
* Configuration
* Documentation
* Prompts
* Tests
* Development logs

The local development environment may also contain a virtual
environment (`Venv/`), which is not part of the repository.

### Persistent Data

Persistent data is stored outside the repository and may include:

* Models
* Memory
* Databases
* Knowledge sources
* Backups

The exact location of persistent data is configured through:

```text
Config/paths.local.yaml
```

which you create from the committed template:
[Config/paths.example.yaml](Config/paths.example.yaml)

This allows the project to remain independent of a specific machine or drive layout.

---

## Installation

### Create a virtual environment

```bash
python -m venv Venv
```

### Activate the virtual environment

#### Windows

```powershell
Venv\Scripts\activate
```

### Upgrade environment packaging tools

`pip-tools` excludes core packaging utilities (`pip` and `setuptools`) from compiled `.txt` manifests as unsafe packages. Baseline environment tooling must be updated to secure versions (addressing `GHSA-xmj3-9gf7-h52w` and `PYSEC-2026-3721`):

```powershell
python -m pip install --upgrade pip "setuptools>=84.1.0"
```

### Install dependencies

```bash
pip install -r requirements.txt
```

---

## Data Structure

The persistent data environment is organized conceptually into the following areas:

```text
Models/
Memory/
Knowledge/
Database/
Backups/
```

### Models

Contains:

* Embedding models
* Local LLMs

### Memory

Contains the project's persistent memory layers, where applicable:

* conversations
* episodic
* semantic
* reflection
* identity
* goals
* values
* working

### Knowledge

Contains external and structured research knowledge, including:

* Obsidian
* Zotero
* Exports

### Database

Contains database-related data such as:

* Cache
* Embeddings
* Vector databases

---

## Configuration

Configuration files are stored in:
[Config/](Config/)

Current configuration files include:

* [`paths.example.yaml`](Config/paths.example.yaml)
* `paths.local.yaml` (not committed)
* [`models.yaml`](Config/models.yaml)
* [`llm.example.yaml`](Config/llm.example.yaml)
* `llm.local.yaml` (not committed; set `provider: "ollama"` locally)
* [`localization.example.yaml`](Config/localization.example.yaml)
* `localization.local.yaml` (not committed; per-domain en/fa overrides)

### `paths.example.yaml`

The committed configuration template. It contains only generic,
portable placeholder values and defines the contract for a valid
paths configuration. It never contains real machine-specific values.

### `paths.local.yaml`

The real, machine-specific configuration for your computer.
It is ignored by git and must never be committed or pushed.

### `models.yaml`

Committed, machine-independent model settings (for example the
active embedding model and its dimension).

### `llm.example.yaml` / `llm.local.yaml`

Copy `Config/llm.example.yaml` to gitignored `Config/llm.local.yaml`
and set `provider: "ollama"` plus your local model name. The tracked
example defaults to `provider: "disabled"`, which stops safely
instead of proceeding. Activate the venv first
(`Venv\Scripts\activate`, or `Venv\Scripts\python -m ...`).

Run either input mode:

```bash
python -m src.living_authenticity.cli --input-file "note.md"
python -m src.living_authenticity.cli --text "my text..."
```

A different cloud API shape is added as one new small module under
`src/living_authenticity/llm/` implementing `LLMProvider`, plus one
registry branch — never by editing the shared interface.

### Configuration workflow

1. Copy the template:

   ```bash
   cp Config/paths.example.yaml Config/paths.local.yaml
   ```

2. Edit `Config/paths.local.yaml` and set the real locations for
   your machine (persistent data root, model path, vector database,
   Obsidian vault, Zotero library, exports, cache, staging, and audit).

3. Set `staging.root` to the single directory that may receive
   controlled-execution artifacts. This is an explicit, config-declared
   exception: `data.root` stays guarded, and staging is still refused
   anywhere else — including arbitrary children of `data.root` and any
   subdirectory of the declared staging root. Leaving it unset
   authorizes no staging location inside guarded persistent data.

4. Set `audit.root` to the single directory that receives persistent
   audit records (append-only traceability, one JSON line per record).
   Primary persistence uses buffered stream writes (`durability="written"`).
   When primary persistence fails, resilient persistence recovers the complete
   run history into emergency journals (`errors/audit_emergency.jsonl`) with
   hardware synchronization (`durability="confirmed"` via `os.fsync`), while
   leaving previously persisted primary records intact.
   It is part of the guarded-root family: it must resolve outside the
   repository and must not overlap the vault, staging, or vector
   database roots. When configured, the audit writer may create the
   root and its category subroots (`passed/`, `failed/`, and `errors/`) if missing; it never creates anything
   outside that boundary. Leaving it unset keeps audit records in
   memory only. Optional `audit.passed` and `audit.failed` select
   independently where successful vs unsuccessful terminal records are
   stored; each must resolve inside `audit.root` (defaults:
   `<audit.root>/passed`, `<audit.root>/failed`, and `<audit.root>/errors`). Storage category
   `failed` includes intentional safety holds/rejections and is not
   synonymous with a system error (operational execution faults and cancellations are partitioned under `errors/`) — richer lifecycle outcomes remain
   on the audit record. The launcher UI exposes these directories as native OS folder picks (`POST /api/folders/pick` and `POST /api/config`)
   without requiring manual YAML editing.

5. Run the test suite (see [Testing](#testing)) to verify your configuration
   loads correctly.

Repository-internal locations — the repository root itself and the
[`Logs`](Logs/) and [`Prompts`](Prompts/) directories — are always derived automatically
from the repository root. They must not be set in
[paths.example.yaml](Config/paths.example.yaml) or `paths.local.yaml`.

If `Config/paths.local.yaml` is missing, the loader falls back to the
generic values in [`paths.example.yaml`](Config/paths.example.yaml). These values are placeholders
and should be replaced with machine-specific paths before using
persistent data.

---

## Dependency Management

The project uses **pip-tools** for dependency management.

### Source dependencies

[requirements.in](requirements.in)

### Resolved dependencies

[requirements.txt](requirements.txt)

### Regenerate dependencies

Dependency regeneration is a development task and requires the dev
dependencies to be installed (`pip install -r requirements-dev.txt`).

```bash
pip-compile requirements.in
```

## Testing

The project uses **pytest**. Dev-only dependencies are managed with
pip-tools separately from the runtime dependencies:

[requirements-dev.in](requirements-dev.in)
[requirements-dev.txt](requirements-dev.txt)

Install the dev dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the full test suite from the repository root:

```bash
python -m pytest
```

The tests run against the real `Config/` directory for the
integration check and against synthetic temporary configurations for
everything else. No production data is read by the test suite.

Three verification states stay distinct (see also [README.md](README.md)):

1. **Synthetic test coverage** — the committed suite above; fixtures
   and temporary directories only, no production data.
2. **Real-environment readiness** — bounded, read-only checks against
   the actual local deployment (config resolution, staging isolation
   guards, local model load, read-only vector-store probe). Results
   are machine-local and never committed.
3. **An authorized real-data run** — a separately bounded operation on
   explicitly selected real data, only with explicit human
   authorization and every existing safety control. Tests and
   readiness checks never imply this state.

### Local-infrastructure tests (opt-in, never in ordinary runs)

`tests/local/` holds machine-specific checks that cannot be expressed
with synthetic values (real model load, local LLM endpoint, bounded
read-only vault read). They skip unless explicitly opted in:

```bash
set LIVINGAUTHENTICITY_RUN_LOCAL=1
python -m pytest tests/local -q
```

Each has a tracked public `.example` counterpart. The ordinary suite
never contacts a model service and never reads the vault.

---

## Bounded Local Integration (optional)

The bounded local-integration runner composes the existing pipeline and
governance contracts over **explicitly supplied** paths. It performs no
discovery of its own: there are no defaults for source, staging, corpus,
Core, or vector-store roots.

Token-overlap retrieval (no local infrastructure required):

```bash
python -m src.living_authenticity.cli \
  --source-root <existing directory of .md/.txt inputs> \
  --staging-root <existing isolated directory> \
  --max-files 1
```

Real local embedding and vector retrieval:

```bash
python -m src.living_authenticity.cli \
  --source-root <existing directory of .md/.txt inputs> \
  --staging-root <existing isolated directory> \
  --max-files 2 \
  --corpus-root <existing directory of known notes> \
  --core-root <existing directory of approved Core references> \
  --embed \
  --vector-db <existing empty directory for the isolated database>
```

Notes:

* `--max-files` defaults to `1` and bounds source, corpus, and Core files
  alike; each additional file is an explicit operator decision.
* Every extracted knowledge unit is governed independently through the
  unified **Human Review & Explicit Approval** boundary: the complete
  proposal, evidence, and uncertainties are displayed for all actions
  (`CREATE`, `NEEDS_REVIEW`, `DO_NOT_IMPORT`), followed by explicit human
  acknowledgment (for non-CREATE) or explicit approval / hash-rebinding
  proposal edit ($H_0 \to H_1$) (default No for `CREATE`), fresh 7-check
  revalidation (including single-target / no-duplicate output verification),
  controlled execution, continuous audit record, and final output assembly.
* `--embed` uses the configured local embedding model
  (`Config/models.yaml` + `Config/paths.local.yaml`) and an isolated
  LanceDB vector database. `--vector-db` is required with `--embed`, must
  resolve outside the repository, and is refused when it resolves to the
  configured production vector database. Active query chunks are embedded
  into a run-scoped `TemporaryEmbeddingCache` that stays in memory only for
  that run and is wiped immediately afterward.
* The corpus is indexed into that isolated database as retrieval context.
  A vector store is a retrieval index, never an authoritative knowledge
  store, and a vector result is evidence only — never identity,
  confidence, or authorization.
* Indexing is refused when the isolated database is not empty. Storing
  the same corpus twice would accumulate duplicate rows and change the
  retrieval evidence while the inputs stayed identical, so the operator
  either reuses the existing index (omit `--corpus-root`), uses the
  human-authorized Vault reconciliation contract (which adds new note
  embeddings, replaces modified note embeddings, and removes deleted note
  embeddings), or supplies a fresh empty `--vector-db` directory.
* The staging root is validated before anything is read: explicit,
  existing, real directory, not a symlink, outside the repository. Inside
  persistent-data roots it is accepted only when it resolves identically
  to the single config-declared `staging.root`; every other
  persistent-data location, and every other child of `data.root`, remains
  rejected.
* The `Core` class holds a single active reference note representing the
  central research thesis of the repository (carrying the highest default
  analytical and retrieval weight, while previous Core versions move to
  `Archive`). With no `--core-root` or when Core is unresolved, proposals
  never hallucinate a cold-start bypass and resolve conservatively to
  `NEEDS_REVIEW` (or safe-stop for human resolution in the UI).
* This is not a vault migration and not an authoritative write. `CREATE`
  means creation into the isolated staging area only.

A synthetic walkthrough lives in
[examples/bounded_local_integration/](examples/bounded_local_integration/README.md).

### Additional input and vault flags

Besides `--source-root`, the CLI also accepts:

* `--input-file <path>` — process a single existing file instead of
  scanning a directory.
* `--text "<text>"` — process a single piece of text supplied directly
  on the command line, with no file read. Exactly one of
  `--input-file`, `--text`, or `--source-root` must be given.
* `--vault-context` — include a small number of bounded, read-only
  excerpts from the configured Vault directly in the LLM prompt
  (informational context, not retrieval evidence).
* `--vault-retrieval` — include the configured Vault as part of the
  retrieval corpus (token-overlap by default; semantic when combined
  with `--embed` and an isolated `--vector-db`). The Vault is always
  read-only: nothing in this path ever writes to it.
* `--verbose` — print the full inspectable intermediate result for
  every candidate (retrieval, comparison, relation, core, classification,
  proposal, confidence, filter, LLM synthesis), not just the summary line.

### Personal Vault vs. Examples mode (Vault Mode vs. Test Mode)

The project distinguishes two operating modes (`knowledge/modes.py`):
Example files and designated test-specific files belong strictly to
**TEST MODE** (`examples`), which uses only the synthetic material under
`examples/` and fails closed rather than ever resolving a configured personal
Vault. **VAULT MODE** (`personal_vault`) means the user selects folders
through the frontend and the application is intended to process the user's
own data from those selected folders. While the current implementation provides
root directory configuration and fail-closed path validation, end-to-end
processing of arbitrary user vaults remains an ongoing integration goal.
The active mode is recorded on every audit record produced during that run.

---

## Running the Application (interim browser launcher)

The delivery method for the current stage is a local loopback launcher
that serves the React/TypeScript presentation layer in the system
browser. A dedicated application window is a separate future packaging
decision.

### 1. Build the frontend (once, and after frontend changes)

```bash
cd frontend
npm install
npm run build
```

### 2. Start the launcher

```bash
python -m src.living_authenticity.adapter
```

The launcher binds `127.0.0.1` only, mints a fresh in-memory launch
token per start (never persisted, never placed in URLs or logs), and
opens the system browser. If `frontend/dist` is missing, a placeholder
page explains that the frontend must be built first.

Transport surface (JSON + SSE, token header on every non-health route):
- `GET /api/health`: Discovery and instance check.
- `GET /api/state`: Snapshot configuration, folder validity, active models, Knowledge Language settings (`app`, `staging`, `audit`), and paused runs.
- `GET /api/journals`: 1:1 paired recent audits and staging records (including `passed/`, `failed/`, `errors/audit.jsonl`, and emergency recovery journals `errors/audit_emergency.jsonl`, with quarantine reporting for unreconciled staging artifacts).
- `GET /api/events`: SSE stream for live DeepThink-style `Current Audit` analytical traces and sanitized operational run events across the 17-step (16 unified-stage) workflow—including the 5 `Input` validation/provenance checks, the 8 discrete `Ingestion` sub-stages (`reader_selection`, `reading`, `cleaning`, `normalization`, `parsing`, `metadata_extraction`, `chunking`, `extraction`), the discrete `Retrieval` operations (`query_preparation`, strategy/method validation, `candidate_retrieval`, `relevance_assessment`, `candidate_identification`, `retrieval_evidence_recording` across `started`, `completed`, and `failed` phases), multi-factor mathematical confidence scores (`0.00`–`1.00`), and synchronized wait/resume during unified `Human Review & Explicit Approval`.
- `GET /api/vault/sync/status`: Inspect current Vault vector index authorization and freshness state (`unauthorized`, `fresh`, `stale`, `revoked`).
- `POST /api/config`: Persist paths, LLM settings, and per-domain localization (`app`, `staging`, `audit`) to `Config/*.local.yaml`.
- `POST /api/folders/pick`: Open native OS folder dialog server-side with guard validation.
- `POST /api/folders/reveal`: Safe file listing of configured directories.
- `POST /api/vault/index`: Execute initial vault vector baseline indexing via BGE-M3 and LanceDB.
- `POST /api/vault/sync/authorize`: Authorize persistent vector index synchronization for configured Obsidian Vault.
- `POST /api/vault/sync/reconcile`: Perform 3-way Vault reconciliation (indexing added notes, replacing modified note embeddings, and deleting removed note embeddings in the dedicated store).
- `POST /api/vault/sync/decide`: Record explicit human operator approval or rejection when pre-retrieval Vault changes are detected (`sync_approval_required`).
- `POST /api/vault/sync/revoke`: Revoke authorization for Vault indexing.
- `POST /api/run`: Submit interactive chat input (`chat_input` text in current scope, with extensible foundation for future file/media formats) for processing through the full 17-step (16 unified-stage) backend pipeline (`Input` → `Ingestion` → `Retrieval` → `Comparison` → `Relation Analysis` → `Core Analysis` → `Classification` → `Proposal Type` → `Confidence` → `Knowledge Filter` → `LLM Synthesis` → `Unified Human Review & Explicit Approval` → `Revalidation` → `Controlled Execution` → `Audit` → `Output`).
- `POST /api/cancel`: Request cooperative cancellation of an active in-flight run at the next safe boundary.
- `POST /api/approve`: Submit explicit human approval or rejection for `CREATE` proposals bound to `proposal_hash` and `query_unit_id`.
- `POST /api/proposal/edit`: Server-side authoritative edit of pending `CREATE` proposal with SHA-256 hash recomputation ($H_0 \to H_1$, superseding prior hash without file corruption).
- `POST /api/run/accept`: Human acknowledgment for non-CREATE proposals (`NEEDS_REVIEW`, `DO_NOT_IMPORT`) bound to `run_id` and `proposal_hash`.
- `POST /api/run/fix`: Error recovery / paused run resumption with corrected input/parameters.
- `POST /api/run/stop`: Cleanly terminate a paused or safe-stopped run.
- `POST /api/translate`: Live on-demand bilingual translation (English ↔ Persian) across `Audit`, `Staging`, and proposal views, returning a structured `TranslationRecord` (`persisted: false`, with `source_hash` staleness detection).
- `POST /api/consent`: Record explicit per-call user consent for cloud provider interactions.

Without a launcher token (for example plain `npm run dev`), the UI stays in fixture/demo mode and performs no backend calls. Run the launcher with:

```bash
python -m src.living_authenticity.adapter
```

---

## Private Project Documents

Some project documents are intentionally kept local and are not committed to the public repository.

They are stored under:

```text
.project/
```

This directory may contain:

* `.project/Project-Vision.md`
* `.project/Current-Summer-Scope.md`

The `.project/` directory is excluded through `.gitignore`.

---

## Coding Agent Access

Coding agents (Cursor, Codex, Claude Code, and others) must not access the production persistent-data tree by default.

The canonical contract is:
[Coding-Agent-Access.md](Coding-Agent-Access.md)

The development workspace should open only the source-code repository, not the persistent data root.

---

## Development Notes

* Source code and persistent data should remain physically separable.
* Persistent data should not be committed to the public repository.
* Local model files should not be committed to the repository.
* Project-specific paths should be configured through
  `Config/paths.local.yaml`, created from
  [`Config/paths.example.yaml`](Config/paths.example.yaml).
* AI models are not downloaded automatically.
* Ollama is used for local LLM inference where configured.
* Obsidian and Zotero serve as external knowledge sources.
* The development environment should remain portable across machines.
* Provider- and model-specific configuration should remain separate from the core Knowledge Management logic.
