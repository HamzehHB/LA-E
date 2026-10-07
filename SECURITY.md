# Security — LivingAuthenticity Environment

**Status:** Authoritative — operational security baseline
**Role:** The per-checkpoint security routine and the verified security state
of this repository. It grants no access and introduces no rules; principles and
authority live in [AI-Governance.md](AI-Governance.md), filesystem and data
access in [Coding-Agent-Access.md](Coding-Agent-Access.md), and the current
implementation boundary in `.project/Current-Summer-Scope.md`.

---

## 1. Current Security Posture (verified)

Facts verified against the repository at this checkpoint:

* No secrets, credentials, API keys, tokens, or private keys exist in any
  tracked file or anywhere in git history (pattern scan against the known
  pattern list in `src/living_authenticity/security/sensitive_data.py` —
  zero matches).
* Git history **does** contain historical machine-specific filesystem paths
  (former `Config/paths.yaml` versions and an older workspace file). This is a
  documented residual risk (§5). No history rewriting is performed by agents.
* Application code performs network calls only inside
  `src/living_authenticity/llm/` via stdlib `urllib`/`http` (local or
  configured endpoint, privacy gate on resolved host, no streaming,
  credentials from environment only, audit stores prompt hash only).
  No dynamic code execution (`eval`/`exec`), no shell-out
  (`subprocess`/`os.system`), and no deserialization of untrusted data.
* The analytical pipeline is analysis-only: it performs no authoritative writes
  to knowledge. Exactly **three** narrowly scoped write surfaces exist, with
  disjoint roots (staging and audit proved by
  `tests/security/test_execution_write_surface.py`; vector isolation proved by
  vector eligibility tests):
  1. **ControlledExecutor** (`knowledge/governance/execution/`) writes exactly
     one approved + revalidated `CREATE` artifact (`<proposal_hash>.md`) into
     an explicitly supplied staging root confined by `PathBoundary`, with no
     overwrite and no authoritative-vault placement;
  2. **AuditWriter** (`knowledge/governance/audit/persistence.py`) appends
     append-only JSONL records under the config-declared `audit.root`,
     partitioned into configurable `passed` / `failed` subroots (defaults
     `<audit.root>/passed` and `<audit.root>/failed`; `passed/` holds
      ONLY the executed-CREATE terminal record, `failed/` holds every
      other terminal record including pre-execution checkpoints).
      It never overwrites,
     deletes, or renames existing records and never writes to vault, staging,
     index, or any other root. Credential-like keys are stripped recursively
     before persistence. When an audit root is configured, the required
     pre-execution audit append must succeed before staging execution;
  3. **Vector/index subsystem** may write only its own derived LanceDB index
     under an explicitly supplied, isolated vector root (never the production
     vector database, never vault/staging/audit). Indexing is explicit
     operator action only — no watcher and no background reindex.
  Current executable action scope is
  `CREATE` / `DO_NOT_IMPORT` / `NEEDS_REVIEW` — everything else is future work.
* Private configuration and private documents are physically outside the
  tracked tree: `Config/paths.local.yaml`, `Config/llm.local.yaml`,
  `.project/`, `.clinerules/`,
  `Venv/`, `.env`, and `Logs/*` are all covered by `.gitignore` and verified
  with `git check-ignore`. `Config/paths.local.yaml` and
  `Config/llm.local.yaml` must **never be committed
  or pushed**.
* The production persistent-data tree is **denied to coding agents by
  default**. Runtime data-processing components follow
  `.project/Local-Paths-Reference.md`; that access never extends to
  development agents, and knowing a path is not permission.
* Reusable runtime security utilities live in `src/living_authenticity/security/`: a default-deny path boundary (`PathBoundary`) and value-safe sensitive-data detection (`find_secrets` / `contains_secret`). `PathBoundary` is wired into the controlled-execution boundary (`knowledge/governance/execution/`) as its staging confinement; it remains unwired into the ingestion/analysis pipeline, which performs no writes.
* The bounded local-integration runner (`knowledge/integration/`) adds no
  new write authority: it composes the existing approval, revalidation,
  controlled-execution, and audit contracts, routing staging writes
  through `ControlledExecutor` and, only when a valid `audit.root` is
  configured and passes the audit guard, traceability appends through
  `AuditWriter`. Its staging eligibility guard is
  computed from the deployment's own configuration
  (`Config/paths.local.yaml` via `Config/settings.py`) — staging must be
  explicitly supplied, already exist, be a real directory, not be a
  symlink, and resolve outside the repository tree, using the same
  `PathBoundary` resolution and normalization as the executor. Inside a
  configured persistent-data root, staging is accepted only when it
  resolves identically to the single, config-declared `staging.root`
  key: `data.root` and every other guarded root stay guarded by default,
  and arbitrary children of `data.root`, subdirectories of the declared
  staging root, symlinks, and case/separator/trailing-slash/relative
  spellings of other locations are all rejected. The guard is checked
  before anything is read and re-checked before each unit; the exact
  validated value is pinned for the whole run. No default, discovered,
  or hardcoded staging location exists.
* Vector-index writes (`--embed --vector-db`) are confined to an
  explicitly supplied, isolated LanceDB directory that must resolve
  outside the repository and is refused when it resolves to the
  configured production vector database. A vector store is a retrieval
  index, never authoritative knowledge, and it authorizes nothing:
  embeddings and vector results are evidence only, and knowledge
  artifacts still reach the world only through `ControlledExecutor`.
  The isolation guard is checked before any indexing occurs.
* The formal LLM stage is mandatory architecture: a disabled,
  unreachable, or invalid provider stops the affected unit safely
  (`llm_safe_stop`, no candidate, no approval, no execution) instead
  of bypassing synthesis. The vault stays read-only and bounded.
* Example inputs under `examples/` are synthetic and fictitious; they
  contain no real knowledge, no real machine path, and no credentials.

---

## 2. Per-Checkpoint Security Checklist (run before every commit)

Steps 1–5 are automated in `tests/security/test_repository_security.py` — keep it green:

1. **Secret scan** — no known secret patterns in tracked files.
2. **Machine-path scan** — the configured production data root and the
   repository's own machine path appear in no tracked file (the actual root
   is defined only in `Config/paths.local.yaml`; synthetic test fixtures use
   invented values).
3. **Ignore rules** — `.gitignore` covers `Config/paths.local.yaml`,
   `Config/llm.local.yaml`, `.project/`, `.clinerules/`, `Venv/`, `.env`,
   `Logs/`, and `tests/local/`; `git check-ignore` confirms every one.
4. **Policy alignment** — this file exists and stays consistent with
   `AI-Governance.md` and `Coding-Agent-Access.md`.
5. **Unsafe-code scan** — no `subprocess`, `os.system`, `eval`, `exec`,
   `__import__`, `shell=True`, or `pickle` in application code.

Then, manually per checkpoint:

6. **Dependency sync** — `requirements*.in` match the imports actually used
   (see §4); compiled files regenerated; test suite green.
7. **Diff review** — `git diff --check` clean; `git status` contains only the
   files this checkpoint claims to change.

---

## 3. Dependency Security

* Workflow: `requirements.in → requirements.txt` and
  `requirements-dev.in → requirements-dev.txt`, compiled with pip-tools.
* `.in` manifests list only dependencies that are **directly imported** by the
  code today. Nothing is added speculatively for possible future use.
* `pip-tools` itself is a development tool: it lives in `requirements-dev.in`,
  not in the runtime manifest.
* A new dependency may be added only by the checkpoint that actually needs it,
  with a one-line justification in that checkpoint's report.

---

## 4. Incident Response (private project)

* Report suspected vulnerabilities **privately to the maintainer** — never in
  public issues, and never with secrets, private paths, or production data in
  the report.
* Exposed secret → revoke/rotate it immediately, then assess exposure. Removing
  material from git history is a maintainer decision; agents never rewrite
  history.
* Production data exposed to an unauthorized actor → determine the scope of
  exposure, stop all processing that touches it, and request human review.

---

## 5. Residual Risks (accepted, maintainer-owned)

* Historical machine-specific paths in git history (see §1). Accepted: they
  reveal a folder layout, not credentials; rewriting history is a
  maintainer-only decision.
* Maintainer actions on GitHub (outside the repository): enable secret scanning
  and push protection; consider branch protection for `main`.
