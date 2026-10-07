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
   It is part of the guarded-root family: it must resolve outside the
   repository and must not overlap the vault, staging, or vector
   database roots. When configured, the audit writer may create the
   root and its category subroots if missing; it never creates anything
   outside that boundary. Leaving it unset keeps audit records in
   memory only. Optional `audit.passed` and `audit.failed` select
   independently where successful vs unsuccessful terminal records are
   stored; each must resolve inside `audit.root` (defaults:
   `<audit.root>/passed` and `<audit.root>/failed`). Storage category
   `failed` includes intentional safety holds/rejections and is not
   synonymous with a system error — richer lifecycle outcomes remain
   on the audit record. A future UI will expose these as folder picks
   without editing YAML manually.

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
* Every extracted knowledge unit is governed independently: the proposal
  is displayed, then human review, explicit approval (default No), fresh
  revalidation, controlled execution, audit record.
* `--embed` uses the configured local embedding model
  (`Config/models.yaml` + `Config/paths.local.yaml`) and an isolated
  LanceDB vector database. `--vector-db` is required with `--embed`, must
  resolve outside the repository, and is refused when it resolves to the
  configured production vector database.
* The corpus is indexed into that isolated database as retrieval context.
  A vector store is a retrieval index, never an authoritative knowledge
  store, and a vector result is evidence only — never identity,
  confidence, or authorization.
* Indexing is refused when the isolated database is not empty. Storing
  the same corpus twice would accumulate duplicate rows and change the
  retrieval evidence while the inputs stayed identical, so the operator
  either reuses the existing index (omit `--corpus-root`) or supplies a
  fresh empty `--vector-db` directory.
* The staging root is validated before anything is read: explicit,
  existing, real directory, not a symlink, outside the repository. Inside
  persistent-data roots it is accepted only when it resolves identically
  to the single config-declared `staging.root`; every other
  persistent-data location, and every other child of `data.root`, remains
  rejected.
* With no `--core-root`, Core relevance stays unresolved and proposals
  resolve conservatively to `NEEDS_REVIEW`. That is correct behaviour.
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

### Personal Vault vs. Examples mode

The project distinguishes two operating modes (`knowledge/modes.py`):
**Personal Vault mode**, which resolves the Vault configured in
`Config/paths.local.yaml`, and **Examples mode**, which uses only the
synthetic material under `examples/` and fails closed rather than ever
resolving a configured personal Vault. The active mode is recorded on
every audit record produced during that run.

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
