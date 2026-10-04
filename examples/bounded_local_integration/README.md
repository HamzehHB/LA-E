# Bounded Local Integration — Synthetic Example

This directory contains **synthetic, fictitious** example input for the
bounded local-integration workflow. Nothing here is real knowledge,
real user content, or a real machine path.

## What the workflow demonstrates

```text
explicit source root
  → ingestion
  → retrieval (over an explicitly supplied corpus)
  → comparison
  → relation analysis
  → Core analysis (over explicitly supplied approved Core units)
  → classification
  → proposal
  → confidence
  → knowledge filter
  → human review
  → explicit approval
  → revalidation
  → controlled execution into an isolated staging root
  → audit record
```

Every boundary is unchanged by this workflow:

* Retrieval ≠ Decision · Similarity ≠ Identity · Classification ≠ Authorization
* Analysis ≠ Execution · Proposal ≠ Approval · Confidence ≠ Authorization
* Relation Detection ≠ Relation Execution · Core Analysis ≠ Core Modification
* Generated Note ≠ Authoritative Note · Proposed Location ≠ Authorized Destination
* CREATE ≠ direct authoritative placement · Knowledge Filter ≠ approval
* Human Review ≠ automatic approval

## What it is not

* It is **not** a vault migration.
* It is **not** an authoritative write.
* It is **not** autonomous knowledge evolution.
* It does **not** implement `UPDATE`, `MERGE`, or `ARCHIVE`.
* Every write still goes through the existing `ControlledExecutor`
  boundary, into staging only, with no overwrite.

## Input

`input/` holds four synthetic files, chosen to exercise different
outcomes:

| File | Intent |
|---|---|
| `01-observation-attention.md` | Straightforward, labelled content (expected: classified, proposal produced) |
| `02-observation-attention-repeated.md` | Near-identical to `01`, for comparison/identity-like evidence |
| `03-method-calibration.md` | Different canonical type, for cross-type retrieval |
| `04-ambiguous-note.txt` | Deliberately unresolved, expected `NEEDS_REVIEW` |

`corpus/` holds two synthetic files of *existing* knowledge, used as the
explicit retrieval corpus (`--corpus-root`) and, with `--embed`, as the
content indexed into the isolated vector database.

`core/` holds one synthetic file of *approved Core reference* content,
used as an explicitly designated Core observation input
(`--core-root`). Core analysis never modifies Core.

## Running it

All paths are supplied explicitly. There is no default and no discovery.

```bash
python -m src.living_authenticity.cli \
  --source-root examples/bounded_local_integration/input \
  --staging-root <path to an existing isolated directory> \
  --max-files 1
```

With the real local embedding and vector infrastructure:

```bash
python -m src.living_authenticity.cli \
  --source-root examples/bounded_local_integration/input \
  --staging-root <path to an existing isolated directory> \
  --max-files 2 \
  --corpus-root examples/bounded_local_integration/corpus \
  --core-root examples/bounded_local_integration/core \
  --embed \
  --vector-db <path to an empty, explicitly supplied directory>
```

The eight explicit inputs of the full form are:

| Input | Meaning |
|---|---|
| `--source-root` | `.md`/`.txt` files to process (bounded by `--max-files`) |
| `--staging-root` | explicit staging destination (guard-validated) |
| `--max-files` | single upper bound for source, corpus, and Core files |
| `--corpus-root` | explicit existing knowledge; indexed into the vector store with `--embed`, used in-memory otherwise |
| `--core-root` | explicit approved Core references, analytical observations only |
| `--embed` | use the real local BGE-M3 service for embeddings |
| `--vector-db` | isolated LanceDB directory (created empty on first use; never the production database) |

`--corpus-root`, `--core-root`, and `--embed` are all optional. Without
them, the command runs the deterministic token-overlap retrieval with
an empty corpus and no Core units.

Staging rules enforced by the run (the guard is not weakened for any
caller):

1. explicitly supplied — never derived from configuration;
2. must already exist and be a real directory;
3. must not itself be a symlink;
4. must resolve outside the repository/workspace tree;
5. inside a guarded persistent-data root it is accepted **only** when it
   resolves identically to the single config-declared `staging.root`;
   every other persistent-data location, and every other child of
   `data.root`, remains rejected;
6. the same captured value is used, unchanged, for every unit;
7. all writes go through the existing controlled-execution boundary.

A placeholder staging location for documentation only — replace it with
your own isolated directory, or with the directory you declare as
`staging.root` in `Config/paths.local.yaml`:

```text
<your-data-root>/Scratch/LocalIntegrationStaging
```

Do **not** use a repository directory, and do **not** declare a
persistent-data location as staging unless it is the single path you
explicitly authorized as `staging.root`.

## Outcome notice

The proposal stage needs resolved evidence before it can propose
`CREATE`:

* with **no** Core units, Core relevance stays unresolved and every unit
  conservatively resolves to `NEEDS_REVIEW`;
* in **token** mode the retrieval corpus must be supplied in memory
  (`--corpus-root`);
* in **`--embed`** mode the corpus is indexed into the isolated vector
  store and retrieval uses real vector similarity — a retrieval result
  is still evidence only, never identity, confidence, or authorization;
* indexing is **refused when the isolated database is not empty**, so a
  repeated run cannot silently duplicate vectors and shift the evidence.

`NEEDS_REVIEW` output is correct behaviour, not a failure. To exercise
`CREATE` and controlled execution, supply `--corpus-root` and
`--core-root` as shown above. `tests/migration/test_example_workflow.py`
also runs this exact example directory end-to-end against a temporary
isolated staging directory.
