# AI-Governance

## LivingAuthenticity Environment

**Document Status:** Authoritative  
**Scope:** AI behavior, authority, permissions, safety, approval, and execution  
**Applies To:** All AI models, agents, coding agents, automations, orchestration systems, and AI-assisted processes interacting with the LivingAuthenticity Environment

---

## 1. Purpose

This document defines the governance rules for the use of AI within the LivingAuthenticity Environment.

Its purpose is to ensure that AI can provide substantial analytical, computational, retrieval, generation, and engineering assistance without becoming the autonomous authority over the project's knowledge.

The central principle is:

> **AI may analyze and propose. Humans retain authority over authoritative knowledge and consequential knowledge-management changes.**

This principle applies regardless of which model, provider, agent, framework, or automation mechanism is being used.

---

## 2. Core Governance Principle

The LivingAuthenticity Environment is a **human-governed system**.

AI systems are computational participants in the environment, not final authorities.

AI may:

- read;
- retrieve;
- parse;
- clean;
- extract;
- classify;
- compare;
- analyze;
- detect possible duplicates;
- detect possible updates;
- detect possible merges;
- detect possible relationships;
- detect possible conflicts;
- evaluate novelty;
- analyze Core relationships;
- generate drafts;
- generate explanations;
- calculate analytical confidence;
- propose actions;
- generate code;
- assist with testing;
- assist with documentation.

AI may not autonomously:

- approve its own proposals;
- modify authoritative knowledge;
- delete authoritative knowledge;
- merge authoritative knowledge;
- archive authoritative knowledge;
- move authoritative knowledge;
- create authoritative relationships;
- change the authoritative type of a knowledge object;
- modify authoritative Core;
- import information directly into the authoritative knowledge system;
- treat its own confidence as permission;
- silently discard information.

The distinction between **analysis** and **authority** is fundamental.

---
# 3. Authority Model

Authority is separated into three conceptual layers:

### 3.1 AI

AI is responsible for:

- computation;
- analysis;
- interpretation;
- retrieval;
- generation;
- comparison;
- proposal;
- explanation.

AI has no inherent authority to make authoritative knowledge changes.

### 3.2 Human

The human is the final authority over:

- acceptance of knowledge;
- rejection of knowledge;
- updates;
- merges;
- archival;
- authoritative relationships;
- Core changes;
- consequential knowledge-management decisions.

### 3.3 Execution System

The execution layer (`Controlled Execution`) is responsible for performing an already-approved, revalidated action within the authorized staging boundary.

It must not independently decide what should happen.

Controlled Execution operates through five governed sub-steps:

1. **Execution Eligibility Check:** Verifies one final time that the action is supported (`CREATE`), explicitly approved by the human, and revalidated, so no mere proposal is ever executed.
2. **Controlled Destination Validation:** Matches the target destination against system security boundaries (`PathBoundary`) so human approval cannot bypass path confinement rules.
3. **Controlled Write:** Writes the artifact exclusively through the single authorized execution path into the approved Staging destination (never directly into the authoritative Obsidian Vault and never overwriting existing files).
4. **Result Verification:** Verifies the actual filesystem outcome and SHA-256 digest of the written file rather than assuming success from command dispatch.
5. **Execution Receipt Recording:** Records the execution receipt, status, and artifact metadata into `Audit` and streams live completion to `Current Audit` (with resilient emergency `fsync` fallback and automatic `.unreconciled/` quarantine if audit persistence fails).

Therefore:

> **Analysis does not imply authority, and execution does not imply decision-making.**

### 3.4 LLM Synthesis

LLM synthesis is one specific instance of AI's "generation" role under
3.1: a formal, mandatory, non-authoritative analytical stage that runs
after the Knowledge Filter and before the unified Human Review & Explicit Approval stage.

LLM synthesis executes through five governed sub-stages:

1. **Bounded Context Preparation:** Prepares only the necessary, authorized, and size-bounded context for the model so synthesis remains strictly confined to relevant evidence.
2. **Evidence and Uncertainty Packaging:** Packages analytical evidence, comparative differences, Core findings, and explicit uncertainties together with the context so the synthesized summary is never detached from real epistemic limitations.
3. **LLM Request:** Sends the bounded, structured request either to a local model invoked mandatorily via a local runner on the system (such as `Ollama`) or to a configured cloud model using environment-only API credentials and an explicit per-call privacy consent gate.
4. **Structured Output Validation:** Validates that the model response strictly adheres to the expected schema and bounds so incomplete or malformed outputs never enter sensitive downstream governance stages.
5. **Synthesis Result Recording:** Records the validated model output, prompt SHA-256 digest, and processing status into `Audit` and streams the live analytical synthesis trace to `Current Audit`.

LLM synthesis may:

- analyze the bounded, structured context already produced by the
  preceding analytical stages;
- propose candidate titles, bodies, suggested types, reasons, and
  uncertainty for human review (respecting the configured `Staging` and `Audit` knowledge languages with on-demand `Translate` capability).

LLM synthesis must NOT:

- authorize any action;
- select or influence an authoritative destination;
- bypass the Knowledge Filter, unified human review & explicit approval, or
  revalidation;
- modify Core;
- modify the Vault;
- change the proposal's action, which is already determined before
  synthesis runs and is never recomputed from LLM output.

If no usable provider is configured, or the provider's output fails
strict validation (timeout, malformed response, schema violation), the
affected unit must stop safely (`llm_safe_stop`) without reaching approval or execution,
surfacing an actionable recovery state in the frontend UI rather than silently proceeding without synthesis.

> **LLM synthesis does not imply authority, and LLM output does not imply approval.**
---
# 4. Proposal vs. Execution

Every consequential AI action must be understood as moving across strictly separated governance states:

### Proposal (`PROPOSAL TYPE`)

AI has consolidated available analytical evidence and produced a structured, non-authoritative proposed action through six governed sub-stages:

1. **Evidence and Analysis Consolidation:** Combines results from Retrieval, Comparison, Relation Analysis, Core Analysis, and Classification into a coherent evidentiary picture.
2. **Action Eligibility Assessment:** Evaluates whether the evidence and governance rules permit proposing a supported action.
3. **Proposal Action Selection:** Selects one operational action state (`CREATE`, `NEEDS_REVIEW`, or `DO_NOT_IMPORT` in the current executable scope; future actions remain non-executable).
4. **Proposed Content Generation:** Generates the exact proposed note content (in the configured `Staging` language with on-demand `Translate` support) for human inspection prior to any execution.
5. **Proposed Destination Identification:** Identifies the recommended destination path for pre-execution review without granting write permission to that path.
6. **Proposal Identity and Hash Recording:** Records the proposal identifier and canonical SHA-256 content fingerprint (`proposal_hash`) so subsequent human approval binds to that exact proposal version.

A proposal is not an executed change.

### Revalidation (`REVALIDATION`)

Right before execution, the system revalidates that the proposal, content, destination, provenance, and human approval remain valid and mutually consistent across seven mandatory checks:

1. **Proposal Identity Validation:** Verifies the proposal is the exact instance approved by the human (or edited by the human via the authoritative proposal editor, never substituted from an external or untrusted source).
2. **Proposal Content and Hash Validation:** Compares current content against the approved `proposal_hash` so any post-approval modification (authorized or unauthorized) is detected and audited.
3. **Target Validation:** Verifies the operation targets the exact intended unit and prevents a single unit from producing duplicate outputs.
4. **Destination Validation:** Re-verifies path safety and eligibility (`PathBoundary`) immediately prior to write.
5. **Provenance Validation:** Verifies that input-to-output provenance remains intact and traceable.
6. **Approval Binding Validation:** Verifies that a valid, non-expired human approval is cryptographically bound to this exact proposal and hash version.
7. **Current State and Eligibility Validation:** Verifies that all runtime prerequisites and eligibility constraints still hold between approval and execution.

### Execution (`CONTROLLED EXECUTION`)

An approved and revalidated `CREATE` proposal is written once into the isolated Staging boundary (`ControlledExecutor`), verified on disk, and recorded with an execution receipt.

Execution requires explicit human authorization and never writes directly into the authoritative Obsidian Vault.

The system must preserve this boundary even when the proposed action has very high confidence.
---
# 5. Unified Human Review and Explicit Approval

In the governed workflow, **Human Review** (Stage 12) and **Explicit Approval** (Stage 13) operate as a **single unified governance stage (`HUMAN REVIEW & EXPLICIT APPROVAL`)**. Every processed candidate is presented to the human in this unified stage—whether the proposed action is `CREATE`, `NEEDS_REVIEW`, or `DO_NOT_IMPORT`—while authoritative knowledge-management execution (`CREATE`) requires explicit human approval.

### Unified Stage Responsibilities

1. **Candidate & Exact Proposal Presentation:** Presents the input, proposed action, proposed class, confidence, destination, and exact Markdown draft so the human knows precisely what is being reviewed or approved.
2. **Proposal, Evidence, and Uncertainty Inspection:** Displays the proposal alongside supporting evidence, comparative differences, Core analysis, and unresolved uncertainties so decisions are never based solely on a summary label.
3. **Approval Eligibility Check:** Verifies whether the proposal is eligible for execution approval (`CREATE`) versus non-executable review acknowledgment (`NEEDS_REVIEW`, `DO_NOT_IMPORT`) so non-executable items never enter the execution authorization path.
4. **Human Decision Input & Explicit Human Approval:** Captures the human's explicit decision (`Approve`, `Edit Proposal`, or `Reject` for `CREATE`; explicit `Acknowledge / Accept` or `Reject` for `NEEDS_REVIEW` and `DO_NOT_IMPORT`) without guessing or inferring intent.
5. **Approval-to-Proposal Binding (with Human Edit Support):** Binds the approval to the exact identifier and SHA-256 `proposal_hash` of the reviewed proposal. Proposal editing remains open to the human: when the human edits a `CREATE` proposal, a new canonical hash ($H_1$) is computed and cleanly replaces the previous hash ($H_0$, recorded via `supersedes:H0`) without corrupting the artifact or state, and fresh explicit approval binds to the updated hash ($H_1$).
6. **Approval State & Human Decision Recording:** Records the human review state and approval binding into `Audit`. Throughout the human review pause, the live `Current Audit` reasoning stream remains synchronized, waiting for the human decision and resuming continuously once recorded.

Approval must be:

- human;
- explicit;
- identifiable;
- scoped;
- recorded;
- cryptographically associated with the exact proposed action and `proposal_hash`.

The system must not interpret any of the following as approval:

- high confidence;
- a high similarity score;
- a likely duplicate;
- a previous approval of another object;
- an implicit assumption;
- an absence of objection;
- an AI-generated statement such as "approved";
- an automated workflow continuation;
- a model's internal confidence;
- a previous decision that is merely similar;
- an acknowledgment (`Accept`) of a `NEEDS_REVIEW` or `DO_NOT_IMPORT` outcome.

AI cannot approve its own proposal.
---

# 6. Approval Scope

Approval applies only to the action and scope that were actually reviewed.

For example:

- Approval to create one note does not authorize creation of other notes.
- Approval to update one note does not authorize updates to related notes.
- Approval to create one relation does not authorize creation of additional relations.
- Approval to merge two objects does not authorize unrelated merges.
- Approval to modify a concept does not authorize modification of Core.

If the proposed action changes materially after approval, the modified action requires review again.

---
# 7. Confidence and Knowledge Filtering Are Not Authorization

Confidence represents analytical support and epistemic certainty.

It does not represent permission.

For example:

```text
confidence = 0.97
```

means that the system considers its analysis highly supported by consistent evidence.

It does not mean:

```text
authorization = true
```

These are separate concepts.

The system must therefore maintain:

```text
confidence
filter_signal
approval
execution
```

as independent states.

### 7.1 Governed Confidence Assessment (`STAGE 9: CONFIDENCE`)

Confidence is evaluated through four governed sub-stages:

1. **Evidence Sufficiency Assessment:** Evaluates how sufficient the collected evidence across Retrieval, Comparison, Relation Analysis, Core Analysis, and Classification is to support the outcome, ensuring information gaps are never hidden.
2. **Uncertainty Assessment:** Identifies ambiguities, contradictions, and analytical limitations so confidence is never overstated.
3. **Confidence Calculation (`EvidenceStrengthGuard` — 3-Layer Multiplicative Epistemic Formula):** Computes a rigorous, non-cosmetic analytical score $C_{\text{final}} \in [0.00, 1.00]$ paired with an ordinal band (`sufficiently_supported`, `weak`, `contradictory_or_unresolved`, `insufficient_evidence`) as the product of three independent epistemic layers:
   $$C_{\text{final}} = E_{\text{base}} \times P_{\text{conflict}} \times D_{\text{uncert}} \in [0.00, 1.00]$$
   - **Layer 1 — Composite Evidence Base ($E_{\text{base}}$):**
     $$E_{\text{base}} = (0.25 \times S_{\text{ret}}) + (0.30 \times R_{\text{suff}}) + (0.25 \times A_{\text{core}}) + (0.20 \times M_{\text{class}})$$
     combining retrieval/comparative distinctness evidence ($S_{\text{ret}}$, calibrated so novel `CREATE` proposals with verified distinctness in `STAGE 4` are not penalized for lacking prior duplicates), chunk provenance & analytical evidence sufficiency ($R_{\text{suff}}$), Core semantic alignment ($A_{\text{core}}$, neutral baseline $0.50$), and classification separation margin ($M_{\text{class}}$).
   - **Layer 2 — Non-Linear Contradiction Penalty ($P_{\text{conflict}}$):**
     $$P_{\text{conflict}} = \left(1 - \max(D_{\text{cand}}, \min(1.0, 1.25 \times D_{\text{core}}))\right)^{2.0}$$
     applying quadratic penalty amplification whenever the input conflicts with existing candidate notes ($D_{\text{cand}}$ in `STAGE 4`) or with the active `Core` note ($D_{\text{core}}$ in `STAGE 6`, amplified by $\lambda = 1.25$).
   - **Layer 3 — Epistemic Uncertainty Discount ($D_{\text{uncert}}$):**
     $$D_{\text{uncert}} = 1 - (0.40 \times U_{\text{rel}} + 0.35 \times U_{\text{core}} + 0.25 \times U_{\text{class}})$$
     discounting confidence proportionally to recorded ambiguities in Relation Analysis ($U_{\text{rel}}$), Core Analysis ($U_{\text{core}}$), and Classification ($U_{\text{class}}$).
4. **Confidence Result Recording:** Records the auditable confidence vector $(C_{\text{final}}, E_{\text{base}}, P_{\text{conflict}}, D_{\text{uncert}})$, ordinal level, evidentiary basis, and uncertainty tuple into `Audit` and streams the live calculation rationale to `Current Audit`.

### 7.2 4-Gate Algorithmic Knowledge Filter (`STAGE 10: KNOWLEDGE FILTER`)

Immediately following Confidence, the `Knowledge Filter` evaluates the input state, conflict vectors, and $C_{\text{final}}$ against system rules through four sub-stages (`Filter Input Assessment`, `Boundary and Eligibility Signal Evaluation`, `Filter Outcome Determination`, `Filter Reason Recording`) powered by the **4-Gate Algorithmic Knowledge Filter Engine**:

1. **Gate 1 — Provenance & Redundancy Gate (`FILTER_BLOCK_REDUNDANT`):** If the provenance chain is incomplete or similarity $S_{\text{ret}} \ge 0.95$ with zero meaningful difference in `STAGE 4` (pure duplication), enforces an automatic downgrade to `DO_NOT_IMPORT` (`do_not_import`).
2. **Gate 2 — Core & Candidate Conflict Veto Gate (`FILTER_CONFLICT_OVERRIDE`):** If the input exhibits substantive conflict with the active `Core` note ($D_{\text{core}} \ge 0.45$) or contradiction with existing candidate notes ($D_{\text{cand}} \ge 0.60$), vetoes direct `CREATE` and **automatically routes the proposal to `NEEDS_REVIEW`** (`hold_for_review`) while attaching latent evolution tags (`LATENT_CONFLICT_CORE`, `LATENT_CONFLICT_CANDIDATE` / `CONTRADICTION`). Conflict-bearing inputs are never silently discarded as `DO_NOT_IMPORT`; they are preserved for human inspection and establish the foundation for future `CONFLICT_RESOLUTION` (`CONTRADICTION`), `UPDATE`, and `MERGE` categories.
3. **Gate 3 — Confidence Threshold Matrix & Latent Evolution Gate:**
   - $C_{\text{final}} < 0.35$ (without substantive conflict or complementary update/merge overlap) $\to$ `LOW_CONFIDENCE_REJECT` (enforces `DO_NOT_IMPORT`).
   - $0.35 \le C_{\text{final}} < 0.70$ (or substantive conflict from Gate 2, or complementary update/merge overlap) $\to$ `MODERATE_CONFIDENCE_HOLD` (caps action at `NEEDS_REVIEW` and attaches `LATENT_UPDATE`, `LATENT_MERGE`, or `LATENT_CONFLICT_*` tags).
   - $C_{\text{final}} \ge 0.70$ (with zero unresolved Core/candidate conflict) $\to$ `HIGH_CONFIDENCE_PASS` (permits `CREATE` to proceed to synthesis and human review).
4. **Gate 4 — Synthesis Context Budget Gate (`PROCEED_TO_SYNTHESIS`):** Forwards all units evaluated by Gates 1–3 (including `NEEDS_REVIEW` and `DO_NOT_IMPORT`) with bounded evidence budgets and policy constraints to `STAGE 11: LLM SYNTHESIS` so the model synthesizes a transparent report for human review without overriding the filter verdict. Like Confidence, a filter verdict is an analytical boundary signal and never grants approval or execution permission.
---
8. Retrieval Is Not Decision-Making

Retrieval systems exist to find potentially relevant existing knowledge and record verifiable retrieval evidence.

### 8.1 Temporary Input Embedding Cache & Human-Authorized Vault Synchronization

1. **Run-Scoped Temporary Input Embeddings (`TemporaryEmbeddingCache`):**
   - When chunks from `Ingestion` are passed to the embedding model (`BGE-M3`) according to the selected retrieval mode, the generated input embeddings are held strictly in an ephemeral, run-scoped memory cache (`cache-<run_id>`) solely for candidate lookup during that run and are wiped immediately upon run completion (`finally:` cleanup).
2. **Human-Gated Vault Vector Database Synchronization (`VaultSyncSession`):**
   - Synchronization between the active Obsidian Vault and the isolated persistent vector database is governed by explicit human authorization:
     - **If Not Authorized by the Human:** The persistent database is not modified, and candidate selection proceeds against the existing database using the temporary input embeddings.
     - **If Explicitly Authorized by the Human:** Before retrieval executes, the system performs a three-part reconciliation against the Vault source:
       1. **New Notes:** Detects newly added notes in the Vault, generates embeddings, and stores them in the vector database.
       2. **Modified Notes:** Detects modified files in the Vault, generates updated embeddings, and replaces the specific outdated embeddings for those files in the database.
       3. **Deleted Notes:** Detects files removed from the Vault source and deletes their corresponding embeddings from the database.
   - Retrieval (`lexical`, `semantic`, and future `hybrid`) then executes against the updated database.

### 8.2 The 6 Governed Retrieval Sub-Stages

1. **Query Preparation:** Constructs a normalized search query from the input unit so lookup is driven by relevant content terms.
2. **Retrieval Method Selection:** Executes the pre-selected retrieval pipeline (`lexical` token overlap or `semantic` vector similarity, with `hybrid` reserved for future expansion) after verifying pipeline and infrastructure readiness.
3. **Candidate Retrieval:** Finds potentially relevant prior knowledge units (including dynamic priority weighting for the active `Core` note).
4. **Relevance Assessment:** Evaluates the relevance/similarity of each match against the input to distinguish weak matches from substantive evidence.
5. **Candidate Identification:** Records a canonical identity key (`<source>:<position>:<unit_id>`) for each retrieved item so similar candidates are never confused with one another or with the input unit.
6. **Retrieval Evidence Recording:** Records the supporting retrieval evidence, matched terms, similarity scores, and cache receipts into `RetrievalResult` and `Audit` (while streaming live reasoning steps to `Current Audit`).

Retrieval may provide:

candidate notes;

candidate concepts;

candidate sources;

candidate relations;

candidate Core material;

semantic neighbors.


Retrieval does not determine:

identity;

duplication;

update;

merge;

relation;

novelty;

correctness;

authorization.


A retrieved candidate is evidence for analysis, not an automatic decision.
---

9. No Fabricated Knowledge References

AI must never invent:

note IDs;

file paths;

note titles;

relation targets;

database identifiers;

source identifiers;

Core entries;

existing knowledge objects.


If a target cannot be verified as an actual existing object, the system must treat it as unresolved.

It must not create a plausible-looking reference merely because one seems likely.

The correct behavior is:

UNRESOLVED
→ STOP
→ REQUEST REVIEW

rather than fabrication.


---
10. Similarity Is Not Identity (`COMPARISON`)

Semantic similarity is useful for finding candidates.

However:

> Similarity does not establish identity.

The `Comparison` stage compares the input unit against each retrieved candidate through six governed sub-stages to identify similarities, differences, and potential contradictions:

1. **Candidate-by-Candidate Comparison:** Compares each retrieved candidate individually against the input so the outcome of one candidate is never falsely generalized to others.
2. **Content Comparison:** Evaluates substantive meaning and content rather than superficial wording or formatting.
3. **Similarity Assessment:** Measures the degree of overlap using explicit criteria without assuming similarity implies identity.
4. **Difference Identification:** Identifies unique or divergent claims between the input and the candidate so meaningful differences are not hidden behind similarity.
5. **Contradiction Detection:** Detects incompatible claims or conflicting information before any action is proposed.
6. **Comparison Evidence Recording:** Records comparison findings, shared terms, and distinct evidence into `ComparisonResult` and `Audit` (streaming the comparative reasoning live to `Current Audit`).

Similarity does not automatically mean:

duplicate;

same concept;

update;

merge;

relation;

contradiction.


Two highly similar pieces of knowledge may still contain meaningful independent differences.

Two differently worded pieces of knowledge may represent the same underlying knowledge.

Decisions must therefore be based on semantic and conceptual analysis rather than similarity scores alone.
---

11. Duplicate Detection

A candidate should be considered a potential duplicate when it substantially repeats existing knowledge without meaningful independent value.

A duplicate proposal should consider:

semantic content;

conceptual identity;

informational value;

context;

evidence;

meaningful distinctions.


If the candidate contributes no meaningful independent knowledge, the proposed action may be:

DO_NOT_IMPORT

A similarity score alone is insufficient evidence for this decision.


---

12. Update Detection

An update occurs when:

1. the candidate represents the same underlying knowledge unit as an existing object; and


2. the candidate contributes meaningful additional information.



Examples of meaningful additions include:

clarification;

correction;

additional evidence;

refinement;

qualification;

newly discovered context.


An update must not silently replace meaningful existing information.

The system should preserve the historical and conceptual continuity of the knowledge object.


---

13. Merge Detection

A merge may be proposed when:

two objects represent the same conceptual unit;

integration provides meaningful value;

the distinctions between the objects can be preserved.


A merge must not be used simply because two objects are similar.

If meaningful distinctions would be lost, the objects should remain separate.

When uncertainty exists:

NEEDS_REVIEW

is preferred over an irreversible or information-destroying merge.


---

14. New Knowledge

A knowledge unit should be proposed as new when it contains meaningful knowledge that is insufficiently represented by existing authoritative knowledge.

A new knowledge proposal should consider:

existing candidates;

conceptual overlap;

novelty;

context;

evidence;

possible relations;

Core implications.


Novelty does not require total semantic uniqueness.

A knowledge unit can be meaningfully new while being related to existing knowledge.


---

15. Keeping Knowledge Separate

Two knowledge objects should remain separate when their differences are meaningful.

Examples include differences in:

meaning;

theoretical position;

context;

evidence;

perspective;

scope;

methodology;

experiential character.


The system must not optimize for fewer notes at the expense of conceptual integrity.

> Knowledge compression is not inherently knowledge improvement.




---
16. Input, Ingestion, and Knowledge Units

### 16.1 Governed Input Stage (`INPUT` — 5 Sub-Stages)

Before ingestion begins, the `Input` stage enforces five entry-gate responsibilities:

1. **Input Source Selection:** Determines whether the input comes from an approved source to prevent unintended processing of unrelated sources. In the current version, only the user's interactive chat field (`chat_input`) is permitted, while laying the architectural foundation for future authorized sources.
2. **Input Type Detection:** Identifies the input modality/format so each type routes to its appropriate reader. In the current version, only `text` in the chat field is permitted, while establishing the extensible foundation for future document and media types (Word `.docx`, `.pdf`, `.yaml`, audio/voice, and images).
3. **Input Validation:** Verifies structural validity (UTF-8 encoding, size bounds, control-character and noise filtering) and enforces a substantive content threshold of **at least 50 letters (`min_letters = 50`)** so under-specified or invalid inputs do not enter downstream analysis.
4. **Source Identification:** Assigns a stable identifier (`input_id: inp-<hash[:12]>`) and SHA-256 `input_hash` (`InputProvenance`) so outputs remain traceable to their origin and future source expansion remains strictly source-identified and bounded.
5. **Initial Safety Checks:** Verifies operating mode (`personal_vault` or `examples`) and path/access boundaries prior to processing.

### 16.2 Governed Ingestion Stage (`INGESTION` — 8 Sub-Stages)

Accepted input is prepared through eight ordered sub-stages:

1. **Reader Selection:** Selects the reader matching the input format from `ReaderRegistry` (with extensible foundation for YAML, PDF, Word, text, voice, and image, while strictly restricting the current runtime version to interactive chat text).
2. **Reading:** Reads the extractable content from the source so downstream stages access actual data.
3. **Cleaning:** Removes technical noise and formatting artifacts while enforcing a strict governance rule: **substantive or important content must never be deleted**.
4. **Normalization:** Harmonizes superficial character and intra-line spacing variations (Unicode NFC) within safe bounds so form differences do not obstruct comparison.
5. **Parsing:** Extracts structural elements such as headings, sections, YAML frontmatter, wiki-links, and tags to preserve structural relationships.
6. **Metadata Extraction:** Extracts context metadata (format, reader, chunk count, timestamps, and input provenance).
7. **Chunking:** Divides long content into manageable segments. Chunking serves two critical architectural roles:
   - enabling a single input to decompose into **multiple independent knowledge units / outputs** where appropriate; and
   - providing bounded segments essential for **lexical, semantic, and future hybrid retrieval**.
8. **Provenance Recording:** Binds extracted `KnowledgeUnit` objects to their initial source and processing path for end-to-end auditability.

### 16.3 Knowledge Unit Multiplicity

One input may contain:

zero knowledge units;

one knowledge unit;

multiple independent knowledge units.


AI must not assume that one input equals one note.

If an input contains multiple independent knowledge units, each unit should be analyzed separately before determining whether they should ultimately be represented together.

The extraction process must preserve:

original text;

meaning;

context;

provenance;

extracted claims;

uncertainty;

open questions;

conflicts.
---

17. Meaning Preservation

Cleaning, parsing, normalization, and restructuring must not silently alter substantive meaning.

The system must distinguish between:

Source Content

What the original material actually says.

Interpretation

What the AI believes the material means.

Derived Knowledge

What is subsequently inferred or developed from the source.

These layers should not be silently collapsed into one another.

If interpretation is uncertain, the uncertainty must remain visible.


---

18. Provenance

Authoritative knowledge must remain traceable to its origin.

Where applicable, the system should preserve:

source type;

source identifier;

source path;

original text;

originating knowledge unit;

derived-from relationships;

processing metadata;

relevant timestamps.


AI must not fabricate provenance.

If provenance is unavailable, the system must represent it as unknown rather than inventing an origin.


---

19. No Silent Data Loss

No processing stage may silently discard meaningful information.

This applies to:

ingestion;

cleaning;

extraction;

classification;

deduplication;

update;

merge;

migration;

archival;

execution.


If information is not imported, modified, merged, or retained as authoritative knowledge, the reason should be represented when relevant.

The system should prefer:

preserve
or
flag

over:

silently discard


---
20. Core Governance (`CORE ANALYSIS`)

Core represents the current approved theoretical reference of the LivingAuthenticity Environment.

Core has a special status because it can influence interpretation of other knowledge.

### 20.1 The 5 Governed Core Analysis Sub-Stages

1. **Core Schema Resolution:** Resolves the valid Core structure from the project's canonical schema so Core identification relies on a single authoritative reference contract.
2. **Relevant Core Context Identification:** Selects the relevant sections and conceptual anchors of the active Core note for contextual evaluation.
3. **Core Relevance Analysis:** Analyzes the semantic relationship between the input unit and Core concepts to clarify its significance for the central knowledge structure.
4. **Conflict or Uncertainty Detection:** Identifies conceptual incompatibilities or unresolved ambiguities so Core tensions are never overlooked.
5. **Core Analysis Result Recording:** Records the outcome and supporting evidence into `CoreAnalysisResult` and `Audit` (streaming the analytical trace live to `Current Audit`).

### 20.2 Single Active Core Note, Dynamic Retrieval Weighting, and Safe-Stop Rules

1. **Single Active Core Note Invariant:**
   - The `Core` class is the sole knowledge class that always holds **one active note** representing the central research anchor of the repository at that time.
   - When the Core note is replaced or updated through human-governed action, the previous Core note moves to the `Archive` class.
   - While the active Core note itself remains open to scholarly challenge, it carries the **highest analytical weight** relative to all other classes.
2. **Dynamic Default Weighting in Retrieval:**
   - The retrieval subsystem dynamically assigns higher default priority/weighting to the active `Core` note and its vectors in the Vault vector database when evaluating incoming query embeddings, automatically tracking updates whenever the Core note or its embeddings change.
3. **No Hallucinated Cold-Start & Safe-Stop Recovery:**
   - Cold-start states must never be bypassed through hallucination or fabricated Core assumptions.
   - If Core Analysis or any other pipeline stage encounters an error or missing prerequisite, the project halts in a deterministic **Safe Stop**, surfaces an actionable **Fix / Retry / Stop** recovery workflow directly in the frontend UI, and waits for human resolution or retry.

Therefore:

Core must be retrieved dynamically from the actual approved Core.

Core must not be hardcoded into AI prompts or processing logic as a permanent substitute.

AI may analyze relationships to Core.

AI may propose Core-related changes.

AI may not autonomously modify Core.
---

21. Core Conflict

If new knowledge appears to conflict with Core, the system must not automatically resolve the conflict.

It should preserve:

the existing Core position;

the new position;

the evidence for each;

the nature of the conflict;

uncertainty where applicable.


The system may classify the relationship as:

supports Core;

challenges Core;

extends Core;

refines Core;

qualifies Core;

contrasts with Core;

potential Core conflict;

insufficient evidence.


A genuine potential Core conflict requires human review.


---
22. Relations (`RELATION ANALYSIS`)

Relations must represent meaningful, defensible semantic or structural relationships between the input and existing knowledge.

### 22.1 Methodological Distinction: Comparison vs. Relation Analysis

Whereas **Comparison** operates like a *correlational analysis* (measuring surface/semantic similarity, shared terms, and differences between two items), **Relation Analysis** operates like a *causal-comparative and structural analysis* (evaluating whether a defensible directional relationship—such as support, dependency, refinement, or contradiction—holds between the input and existing knowledge).

### 22.2 The 5 Governed Relation Analysis Sub-Stages

1. **Relation Candidate Identification:** Identifies which retrieved items plausibly hold a relationship with the input so analysis focuses strictly on relevant candidates.
2. **Relationship Evidence Analysis:** Weighs evidence for and against a potential relationship so relations are never inferred merely from similarity.
3. **Relation Type Assessment:** Determines the specific nature of the relationship so distinct relation types (e.g., support, dependency, or conflict) are not conflated.
4. **Uncertainty Assessment:** Identifies ambiguity and insufficient evidence so tentative relationships are never presented as established facts.
5. **Relation Result Recording:** Records the relationship findings, evidence, and uncertainties into `RelationDetectionResult` and `Audit` (streamed live to `Current Audit`).

Supported relation categories include:

supports

challenges

extends

refines

qualifies

contrasts

depends_on

derived_from

evidence_for

evidence_against

methodological_relation

conceptual_relation

historical_relation

contextual_relation


A relation requires:

1. an actual source object;


2. an actual target object;


3. a meaningful relationship;


4. sufficient justification.



The system must never create a relation merely because two objects are similar.
---

23. Relation Targets

Every relation target must be resolved against actual existing knowledge.

The system must not generate:

target_id = guessed_id

or:

target_path = guessed_path

If the target cannot be verified:

NO_RELATION

or:

NEEDS_REVIEW

should be considered instead.


---
24. Classification and Action States (`STAGE 7: CLASSIFICATION` & `STAGE 8: PROPOSAL TYPE`)

### 24.1 Governed Classification (`STAGE 7: CLASSIFICATION` — 4 Sub-Stages)

Following Retrieval, Comparison, Relation Analysis, and Core Analysis, the `Classification` stage places the input unit into an evidence-informed proposed category (`Core`, `Concept`, `Observation`, `Experience`, `Research`, `Method`, `Source`, `Meta`, `Archive`) through four governed sub-stages:

1. **Classification Evidence Collection:** Gathers all upstream analytical evidence relevant to determining the unit's knowledge class.
2. **Classification Assessment:** Matches the collected evidence against canonical classification criteria so categorization follows a deterministic structure.
3. **Classification Uncertainty Assessment:** Evaluates ambiguity between competing candidate classes ($U_{\text{class}}$) and separation margin ($M_{\text{class}}$) so uncertain units are never forced into a category with false certainty.
4. **Classification Result Recording:** Records the proposed class, rationale, margin, and uncertainty into `ClassificationResult` and `Audit` (streaming the reasoning live to `Current Audit`).

### 24.2 Canonical Action States & Latent Evolution Foundation (`STAGE 8: PROPOSAL TYPE`)

The canonical knowledge-management actions are:

CREATE
UPDATE
MERGE
DO_NOT_IMPORT
ARCHIVE
NEEDS_REVIEW
NO_ACTION

*(In the current implementation scope, only `CREATE`, `NEEDS_REVIEW`, and `DO_NOT_IMPORT` are active operational proposal outcomes. Cases involving conflict with `Core` or existing candidate notes, or requiring `UPDATE`, `MERGE`, or `ARCHIVE_CORE`, are routed safely to `NEEDS_REVIEW` and tagged with `LATENT_CONFLICT_CORE`, `LATENT_CONFLICT_CANDIDATE` / `CONTRADICTION`, `LATENT_UPDATE`, or `LATENT_MERGE` so future `UPDATE`, `MERGE`, and `CONFLICT_RESOLUTION` / `CONTRADICTION` execution categories can be activated directly from this foundation without breaking hash or approval contracts).*

CREATE

Create a new authoritative knowledge object in Staging after explicit human approval and revalidation.

UPDATE

Modify an existing knowledge object after approval (routed to `NEEDS_REVIEW` with `LATENT_UPDATE` in current scope).

MERGE

Integrate two or more knowledge objects while preserving meaningful distinctions (routed to `NEEDS_REVIEW` with `LATENT_MERGE` in current scope).

DO_NOT_IMPORT

Do not import the candidate into the knowledge creation flow.

ARCHIVE

Move an existing authoritative object into archival status.

Archival is not deletion.

NEEDS_REVIEW

The evidence or interpretation is insufficient, or a substantive conflict (`LATENT_CONFLICT_CORE`, `LATENT_CONFLICT_CANDIDATE` / `CONTRADICTION`) or update/merge overlap requires human inspection.

NO_ACTION

No authoritative modification is currently justified.
---

25. Fail-Safe Behavior

When the system encounters uncertainty that could affect authoritative knowledge, it must fail safely.

Preferred behavior:

detect uncertainty
→ preserve information
→ explain uncertainty
→ stop consequential execution
→ request human review

Not:

detect uncertainty
→ guess
→ execute

Ambiguity must never be converted into false certainty merely to keep a pipeline running.


---

26. AI Permissions

Read Access

AI may be permitted to read information necessary for:

analysis;

retrieval;

comparison;

classification;

research;

testing;

generation.


Read access does not imply write authority.

Analytical Access

AI may:

process data;

calculate similarity;

generate embeddings;

classify;

compare;

identify candidates;

produce proposals.


Write Access

Write access to authoritative knowledge must be controlled.

The ability of an AI system to technically write to a location must never be treated as permission to do so.

Execution Access

Execution must be restricted to explicitly approved actions.


---

27. Production vs. Testing

Production knowledge should not be used as an unrestricted sandbox for autonomous experimentation.

Testing should preferably use:

synthetic data;

fixtures;

copies;

sandbox environments;

controlled test vaults.


Experiments must not silently modify authoritative production knowledge.


---

28. Autonomous Agents

Any autonomous or semi-autonomous agent operating within the environment remains subject to the same governance rules.

An agent does not gain additional authority merely because it:

has more tools;

has longer context;

can execute shell commands;

can edit files;

can access databases;

can call other models;

can operate continuously;

can delegate tasks to other agents.


Capability is not authority.


---

29. Multi-Agent Systems

When multiple agents cooperate, governance applies to the entire system.

Delegation does not bypass approval.

For example:

Agent A proposes
→ Agent B validates
→ Agent C executes

does not authorize execution unless the required human approval exists.

An agent cannot delegate an unauthorized action to another agent and thereby make it authorized.

The same authority boundary applies across the entire agent chain.


---

30. Model and Provider Independence

Governance must remain independent of:

OpenAI;

Anthropic;

Google;

xAI;

local models;

cloud models;

embedding providers;

coding agents;

orchestration frameworks;

future providers.


A more capable model does not receive greater authority automatically.

A less capable model does not receive a different governance principle.

The governance layer must remain stable while models and providers can change.

AI synthesis capability (bounded analytical role): where the current
implementation invokes model-backed synthesis, that capability is a
replaceable analytical collaborator operating under the surrounding
deterministic contracts. It produces candidate outputs for human
review; it never approves, authorizes, executes, or evolves
knowledge. The distinctions of §48 apply to its outputs without
exception: AI analysis is not authorization, AI output is not
approval, AI confidence is not permission, and AI failure never
permits bypassing the mandatory step — the affected unit stops safely
instead.


---

31. Coding Agents

Coding agents are governed by the same authority principles.

A coding agent may assist with:

implementation;

refactoring;

debugging;

testing;

documentation;

architecture analysis;

code review.


However, access to the project's filesystem or repository does not automatically authorize modification of authoritative knowledge.

When working with Knowledge Management infrastructure, coding agents must respect:

schema constraints;

governance boundaries;

approval requirements;

production/test separation;

provenance;

no-silent-loss rules.

### 31.1 Coding agent filesystem access

Coding agents must not, by default, read, write, list, search, index, or mount the production persistent-data tree.

The canonical, tool-independent contract is:

`Coding-Agent-Access.md`

That contract applies to every coding agent, regardless of vendor or product.

Application models used to analyze or filter notes are not coding agents and are outside that filesystem denial.

Explicit human approval, in the same request and limited to a named path, is required before a coding agent may inspect any part of production data.



---

32. Automation

Automation may execute predefined technical operations when those operations do not cross an authority boundary.

However, automation must not silently transform an analytical proposal into an authoritative decision.

A scheduled or automated process must not gain authority simply because it runs without human interaction.

Consequential knowledge-management actions remain subject to explicit approval.


---
33. Auditability, Live `Current Audit`, Knowledge Language, and Output (`STAGE 15: AUDIT` & `STAGE 16: OUTPUT`)

Consequential processing must be continuously traceable from the moment input is received until the final output is presented.

### 33.1 The 9 Governed Audit Sub-Capabilities (`STAGE 15 [FORMERLY 16]: AUDIT`)

`Audit` synchronizes across every stage from start to finish through nine recording capabilities:

1. **Input and Provenance Recording:** Records input metadata, source type, `input_id`, and `input_hash` (never raw private input text) so the origin of the run is verifiable.
2. **Analysis and Proposal Recording:** Records retrieval candidates, comparisons, relations, Core analysis, classification, and the final proposal so the analytical path is reconstructible.
3. **Confidence and Filter Outcome Recording:** Records the 3-layer confidence vector $(C_{\text{final}}, E_{\text{base}}, P_{\text{conflict}}, D_{\text{uncert}})$, the 4-Gate Knowledge Filter verdict, and any attached latent evolution tags (`LATENT_CONFLICT_CORE`, `LATENT_CONFLICT_CANDIDATE`, `LATENT_UPDATE`, `LATENT_MERGE`) so limitations and continuation/stop reasons are explicit.
4. **LLM Result Recording:** Records the LLM provider/model metadata, prompt hash, and validated synthesis result so the model's contribution is traceable.
5. **Human Review Recording:** Records the human inspection and review interaction (`human_review_state`).
6. **Approval State Recording:** Records explicit approval, rejection, or authorized human proposal edit ($H_0 \to H_1$) and its cryptographic binding to `proposal_hash`.
7. **Revalidation Recording:** Records the 7 pre-execution revalidation checks confirming that execution prerequisites were verified again after approval.
8. **Execution Result Recording:** Records the actual `ControlledExecutor` outcome, artifact path, and SHA-256 digest, keeping execution strictly distinct from proposal.
9. **Error or Failure Recording:** Records safe stops, validation rejections, emergency persistence receipts, and artifact quarantine events across `passed/`, `failed/`, and `errors/` partitions so failures are never hidden.

Audit records are append-only (`AuditWriter`) and must never be silently rewritten to hide previous decisions. Audit itself never issues approval or execution permission.

### 33.2 The 5 Governed Output Sub-Capabilities (`STAGE 16 [FORMERLY 17]: OUTPUT`)

The `Output` stage presents the final state of the workflow clearly to the researcher while keeping analysis, proposal, approval, and actual execution distinct:

1. **Final Status Assembly:** Consolidates outcomes from all stages into a coherent run summary (`processed`, `executed`, `held`, `terminal_outcome`, and bijective audit receipt verification).
2. **Processing Result Presentation:** Displays the analytical outcome and recorded human decisions in `ProcessingResult`.
3. **Staging Artifact Reference:** Provides the file location, identifier, and SHA-256 hash of any created Staging artifact (paired 1:1 in the `Recent Staging` journal).
4. **Audit Reference:** Provides direct traceability to the corresponding `Audit` record in the `Recent Audits` journal.
5. **Error or Stop Explanation:** Clearly explains the reason for any stop, error, or clarification request, exposing in-UI `Fix / Edit Input`, `Retry`, and `Stop` controls. Displaying an output result never implies new approval or automatic placement into the authoritative Obsidian Vault.

### 33.3 The Cross-Cutting Verification Triad Across All 16 Stages

Every stage in the workflow (`STAGE 1: INPUT` through `STAGE 16: OUTPUT`) must enforce the **Cross-Cutting Verification Triad**:

1. **`Continuous Cognitive Audit Hook (DeepThink Stream)`:**
   - Acts as the internal connector across all sub-stages within each stage and as the continuous bridge linking each stage to the previous and next stages in the workflow.
   - Connects simultaneously to persistent `Audit` and to the live **`Current Audit`** panel, streaming an authentic, verifiable **DeepThink-style analytical thought process** (explaining the evidence being weighed, the constraints checked, and the reasoning in real time—not merely emitting bare event labels). During `STAGE 12: HUMAN REVIEW & EXPLICIT APPROVAL`, `Current Audit` pauses in sync with the human review and resumes seamlessly upon human decision.
2. **`Knowledge Language Alignment & Translation Layer`:**
   - Coordinates both `Audit` and `Staging` with the user's selected `Knowledge Language` settings (`language.audit` and `language.staging` in `en` or `fa`), ensuring that audit narratives and staged notes are written and recorded directly in their designated domain language.
   - Provides an interactive **`Translate`** capability (`/api/translate`) across `Audit`, `Staging`, `Current Audit`, and `ProcessingResult` so any content can be translated on demand between English and Persian (`TranslationRecord` with SHA-256 `source_hash` verification).
3. **`Frontend State & UI Sync Verification`:**
   - Ensures every stage, sub-stage pill, live reasoning trace, language selector, translation toggle, and Safe-Stop error recovery control is fully wired and synchronized with the frontend UI.
---

34. Reversibility

Where technically possible, consequential changes should be reversible.

The system should favor workflows that preserve:

previous versions;

provenance;

audit history;

source material;

rejected proposals;

archived states.


Irreversible operations require greater caution and explicit approval.


---

35. Separation of Concerns

The following responsibilities should remain conceptually separate:

Models

Perform computation and inference.

Memory

Provides storage, indexing, embeddings, and retrieval infrastructure.

Retrieval

Finds actual candidate knowledge.

Knowledge Management

Analyzes knowledge and proposes structural actions.

Execution

Applies explicitly approved changes.

Governance

Defines authority, permissions, safety, and approval boundaries.

No component should silently assume the authority of another component.


---

36. Knowledge vs. Memory

Memory infrastructure is not automatically authoritative knowledge.

A stored vector, embedding, cache entry, retrieval result, database record, or temporary representation does not become authoritative merely because it exists.

Likewise, a retrieved item is not necessarily:

correct;

current;

authoritative;

relevant;

identical to the current input.


Authoritative status must be determined through the governed Knowledge Management process.


---

37. Knowledge vs. Model Output

Generated model output is not automatically project knowledge.

A model response becomes authoritative knowledge only through the governed process.

Therefore:

model output
≠
authoritative knowledge

A generated statement may be:

an analysis;

a hypothesis;

a proposal;

a draft;

an interpretation;

a candidate knowledge unit.


Its status must remain explicit.


---

38. Uncertainty

The system must represent meaningful uncertainty.

Uncertainty may arise from:

ambiguous source text;

incomplete context;

insufficient retrieval;

competing interpretations;

uncertain identity;

uncertain relation;

uncertain Core interaction;

insufficient evidence.


Uncertainty should not be hidden simply because the system requires a single output.

When uncertainty materially affects the decision, the correct state is normally:

NEEDS_REVIEW


---

39. Human Review as a First-Class State

Human review is not an error condition.

It is a legitimate and necessary state of the system.

A review request should provide enough information for the human to understand the proposed decision, including where relevant:

original input;

extracted knowledge;

proposed type;

retrieved candidates;

evidence;

duplicate analysis;

update analysis;

merge analysis;

novelty analysis;

relation analysis;

Core analysis;

proposed destination;

generated note;

proposed action;

confidence;

reasoning;

provenance.


The goal is not merely to ask:

> "Approve?"



The goal is to make the decision understandable and reviewable.


---

40. Decision Transparency

For consequential proposals, AI should be able to explain:

what it found;

what it believes the knowledge means;

which existing objects it compared;

why they were considered relevant;

why a duplicate/update/merge/new decision was proposed;

why a relation was proposed;

how Core is involved;

what uncertainty remains;

what action is being proposed;

what evidence supports the proposal.


Explanations must not be fabricated after the fact.


---

41. No Hidden Governance

Governance rules must not depend exclusively on:

hidden model prompts;

undocumented assumptions;

temporary agent instructions;

provider-specific behavior;

implicit conventions.


Critical authority boundaries should be represented explicitly in the system architecture and validation logic.


---

42. Security Principle

The minimum necessary authority principle should be preferred.

An AI system should receive only the access required for its current task.

For example:

read-only analysis

should not require:

authoritative write access

and:

proposal generation

should not require:

execution authority

Technical capability should be narrower than or equal to the intended authority whenever practical.


---

43. Permission Escalation

AI must not escalate its own permissions.

It must not:

grant itself write access;

modify permission controls;

bypass approval;

disable governance checks;

remove audit requirements;

change safety constraints;

redefine its own authority.


If additional permissions are required, the request must be surfaced for human control.


---

44. Governance Integrity

Governance mechanisms themselves are protected.

An AI system must not modify governance rules merely because they interfere with a desired action.

Changes to governance must be treated as consequential project-level changes and require explicit human control.


---

45. General Decision Rule

For any knowledge-management decision, the system should reason in this order:

1. What is the input?
2. What independent knowledge units does it contain?
3. What does each unit mean?
4. What existing knowledge actually exists?
5. Which existing objects are relevant candidates?
6. Is the candidate genuinely the same knowledge?
7. Is it a duplicate?
8. Is it an update?
9. Is a merge justified?
10. Is it meaningfully new?
11. Are there genuine relations?
12. How does it relate to current Core?
13. Is there a conflict?
14. What destination is appropriate?
15. What action is being proposed?
16. How confident is the analysis?
17. What uncertainty remains?
18. Has the human explicitly approved the action?
19. If approved, can the exact approved action be safely executed?
20. Can the result be audited?


---
46. Canonical Processing Pattern and Architecture Logic

### 46.1 Canonical Governed Workflow Pattern (16 Unified Stages)

The general governed pattern across the 16 unified stages (with Stages 12 & 13 merged into `STAGE 12: HUMAN REVIEW & EXPLICIT APPROVAL`) is:

```text
STAGE 1: INPUT
     ├── Input Source Selection (chat_input active; extensible foundation)
     ├── Input Type Detection (text active; extensible for Word/PDF/YAML/audio/image)
     ├── Input Validation (>= 50-letter threshold & structural bounds)
     ├── Source Identification (input_id & provenance)
     └── Initial Safety Checks
  ↓
STAGE 2: INGESTION
     ├── Reader Selection
     ├── Reading
     ├── Cleaning (zero deletion of substantive content)
     ├── Normalization
     ├── Parsing
     ├── Metadata Extraction
     ├── Chunking (1-to-many unit decomposition & lexical/semantic/hybrid retrieval readiness)
     └── Provenance Recording
  ↓
STAGE 3: RETRIEVAL
     ├── Ephemeral Input Embedding Cache (wiped at end of run)
     ├── Human-Gated Vault Sync (if authorized: add new, replace modified, delete removed vectors)
     └── Query Preparation → Method Selection & Health Check → Candidate Retrieval → Relevance Assessment → Candidate Identification → Evidence Recording
  ↓
STAGE 4: COMPARISON
     └── Candidate-by-Candidate → Content → Similarity → Difference → Contradiction → Evidence Recording
  ↓
STAGE 5: RELATION ANALYSIS
     └── Candidate Identification → Evidence Analysis → Type Assessment → Uncertainty Assessment → Result Recording
  ↓
STAGE 6: CORE ANALYSIS
     └── Core Schema Resolution → Context Identification → Relevance Analysis (single active Core note with dynamic retrieval weight) → Conflict/Uncertainty Detection → Result Recording
  ↓
STAGE 7: CLASSIFICATION
     └── Evidence Collection → Classification Assessment → Uncertainty Assessment → Result Recording
  ↓
STAGE 8: PROPOSAL TYPE
     └── Consolidation → Eligibility Assessment → Action Selection (CREATE / NEEDS_REVIEW / DO_NOT_IMPORT) → Content Generation → Destination Identification → Identity & Hash Recording
  ↓
STAGE 9: CONFIDENCE
     └── Evidence Sufficiency → Uncertainty Assessment → 3-Layer Multiplicative Formula (C_final = E_base * P_conflict * D_uncert) → Result Recording
  ↓
STAGE 10: KNOWLEDGE FILTER
     └── Filter Input Assessment → 4-Gate Algorithmic Evaluation (Gate 1 Redundancy, Gate 2 Core/Candidate Conflict -> NEEDS_REVIEW Veto, Gate 3 C_final Matrix & Latent Tagging, Gate 4 Synthesis Budget) → Filter Outcome Determination → Filter Reason Recording
  ↓
STAGE 11: LLM SYNTHESIS
     └── Bounded Context Preparation → Evidence & Uncertainty Packaging → Mandatory Local (Ollama) / Privacy-Gated Cloud Request → Structured Output Validation → Synthesis Result Recording
  ↓
STAGE 12 [MERGED 12 & 13]: HUMAN REVIEW & EXPLICIT APPROVAL
     └── Candidate & Exact Proposal Presentation (all actions) → Evidence & Uncertainty Inspection → Approval Eligibility Check → Human Decision Input (Acknowledge for non-CREATE; Approve/Reject/Edit with H0->H1 hash rebinding for CREATE) → Approval State Recording (with synchronized live Current Audit pause/resume)
  ↓
STAGE 13 [FORMERLY 14]: REVALIDATION
     └── Proposal Identity → Content & Hash → Single-Target (no duplicate outputs) → Destination → Provenance → Approval Binding → Current State & Eligibility
  ↓
STAGE 14 [FORMERLY 15]: CONTROLLED EXECUTION
     └── Execution Eligibility Check → Controlled Destination Validation → Controlled Write (Staging only) → Result Verification → Execution Receipt Recording
  ↓
STAGE 15 [FORMERLY 16]: AUDIT (Continuous Spine across all stages + Live DeepThink-style Current Audit)
     └── Input/Provenance → Analysis/Proposal → Confidence/Filter → LLM Result → Human Review → Approval State → Revalidation → Execution Result → Error/Failure Recording
  ↓
STAGE 16 [FORMERLY 17]: OUTPUT
     └── Final Status Assembly → Processing Result Presentation → Staging Artifact Reference → Audit Reference → Error or Stop Explanation
```

This is a governance pattern, not a requirement that every implementation use identical software components.

### 46.2 Architecture Logic at a Glance

Each pair of stages answers a distinct question in the governed research cycle:

- **STAGES 1 & 2 (`INPUT` & `INGESTION`):** What has been received, and how is it prepared for processing without losing meaning?
- **STAGES 3 & 4 (`RETRIEVAL` & `COMPARISON`):** What relevant evidence exists in prior knowledge, and what similarities, differences, or contradictions does the input have with each candidate?
- **STAGES 5 & 6 (`RELATION ANALYSIS` & `CORE ANALYSIS`):** What defensible relationships exist, and how does the input relate to the central conceptual Core of the repository?
- **STAGES 7, 8, 9 & 10 (`CLASSIFICATION`, `PROPOSAL TYPE`, `CONFIDENCE` & `KNOWLEDGE FILTER`):** What is the nature of the input, what action is proposed, how strong is the mathematical evidence support ($C_{\text{final}}$), and what 4-gate boundary rules or conflict downgrades apply?
- **STAGE 11 (`LLM SYNTHESIS`):** How can the evidence, analyses, and uncertainties be dynamically synthesized into a structured summary without granting decision authority to the model?
- **STAGE 12 (`HUMAN REVIEW & EXPLICIT APPROVAL` — MERGED 12 & 13):** What is the human inspecting, and to what exact proposal version is execution permission granted?
- **STAGES 13 & 14 (`REVALIDATION` & `CONTROLLED EXECUTION` — FORMERLY 14 & 15):** Are all conditions still valid right before execution, and is the authorized operation performed in a strictly controlled manner?
- **STAGES 15 & 16 (`AUDIT` & `OUTPUT` — FORMERLY 16 & 17):** What happened across the entire reasoning chain, why did it happen, and what is the final outcome?

> **Central LA-E Principle:** The system must be able to analyze knowledge and make proposals, but it must never confuse analysis, similarity, classification, confidence, or even model-generated output with authorization to modify authoritative knowledge. Every consequential action must pass through the boundaries of human review, explicit approval, revalidation, and controlled execution, and be durably documented in Audit.
---

47. Default Safe State

When the system cannot determine whether an action is safe and authorized, its default state must be:

STOP_AND_REQUEST_REVIEW

The system should never prefer an unauthorized action merely because it allows the pipeline to continue.


---

48. Governance Invariants

The following principles are non-negotiable:

1. Human authority is final.


2. AI analysis is not authorization.


3. AI cannot approve its own proposal.


4. Confidence is not permission.


5. Retrieval is not decision-making.


6. Similarity is not identity.


7. Similarity is not duplication.


8. Similarity is not a relation.


9. Existing targets must be verified.


10. AI must never invent knowledge references.


11. Meaning must be preserved.


12. Provenance must be preserved.


13. Meaningful distinctions must not be silently erased.


14. There must be no silent data loss.


15. Core must be treated as an approved reference, not an autonomous learning target.


16. Core conflicts require human review.


17. Authoritative changes require explicit approval.


18. Execution must match the approved action.


19. Auditability must be preserved.


20. Ambiguity must result in review rather than fabrication.


21. Agents cannot bypass governance through delegation.


22. Models and providers do not determine authority.


23. Technical access does not equal permission.


24. Governance rules cannot be bypassed by automation.


25. When in doubt, stop and request review.




---
49. Final Principle

The LivingAuthenticity Environment is designed to use AI as a powerful computational and intellectual instrument without surrendering human authority over the knowledge it is built to preserve and develop.

The intended relationship is therefore:

```text
Human
  ↓
Authority
  ↓
Governed AI
  ↓
Input / Ingestion / Retrieval / Analysis / Confidence / Filter / LLM Synthesis / Proposal
  ↓
Unified Human Review & Explicit Approval
  ↓
Revalidation & Controlled Execution (Staging)
  ↓
Continuous Audit & Truthful Output State
```

The system should maximize what AI can understand, analyze, retrieve, compare, generate, and propose, while strictly controlling what AI can decide and change.

> AI can help build the knowledge system. It does not become the authority of the knowledge system.
