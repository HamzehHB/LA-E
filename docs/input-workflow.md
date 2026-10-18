# Input Stage Architecture and Recovery Workflow

**Module:** `src.living_authenticity.knowledge.input.processor`  
**UI Components:** `Composer`, `ProcessingResult` (`frontend/src/components/Panels.tsx`)  
**Adapter:** `src.living_authenticity.adapter.app`  
**Status:** Authoritative Foundation  

---

## 1. Overview and Responsibility

The Input stage is the single authoritative entry gate of the LivingAuthenticity Knowledge Management loop. It provides an ergonomic, zero-latency in-UI chat composer for the researcher, backed by rigorous backend validation, noise detection, privacy guards, and an interactive recovery workflow.

```text
[Composer UI] (In-place, UTF-8 byte check, draft preservation)
     │
     ▼ POST /api/run
[Adapter Layer] (Synchronous schema validation, token check)
     │
     ▼ process_input()
[Input Processor]
     ├── Source Selection ('chat_input')
     ├── Operating Mode ('personal_vault' / 'examples')
     ├── Type & Encoding (UTF-8, <= 1MB, no raw control chars)
     ├── Noise Boundaries (Punctuation-only, spam, fragment noise)
     └── Clarification Detection (Ambiguous single tokens)
     │
     ├─► ACCEPTED ────────────► Hand-off to Ingestion & Retrieval
     ├─► NEEDS_CLARIFICATION ─► Halt, Partition to failed/ audit, Surface Clarification in UI
     ├─► REJECTED ────────────► Halt, Partition to failed/ audit, Surface Error in UI
     └─► STOPPED ─────────────► Halt, Partition to failed/ audit, Surface Stopped in UI
```

---

## 2. In-UI Chat Composer

* **Positioning:** Full-width and bottom-anchored, staying firmly in place without jumping or transforming into a detached floating modal on focus.
* **Typing Experience:** In-place multiline editing with standard `Enter` (submit) and `Shift+Enter` (new line) shortcuts. Auto-grows up to 120px with smooth scrolling.
* **Exact Placeholder:**
  * English: `Type or paste here (text only — no uploads yet)`
  * Persian: `متن را اینجا بنویسید یا بچسبانید (فقط متن — بدون بارگذاری فایل)`
* **Immediate Local Feedback:** Pure client-side UTF-8 byte calculation and character count badges without latency. Submissions exceeding 1,000,000 bytes or containing only whitespace are blocked client-side.
* **Offline Draft Retention:** If the backend launcher is disconnected, the composer retains the typed text intact (no wiping) and surfaces an actionable connection banner and recovery panel.

---

## 3. Backend-Authoritative Evaluation Boundary

Client-side checks never substitute for backend authority. Every input submitted to `/api/run` is independently evaluated by `process_input()`:

### Evaluation Attributes
* **Input Identification:** Computes deterministic SHA-256 hash (`content_hash`) and assigns stable `input_id` (`inp-<hash[:12]>`).
* **Source Selection:** Rejects any interactive source other than `chat_input`.
* **Mode Verification:** Ensures source mode is strictly `personal_vault` or `examples`.
* **Structural Bounds:** Enforces UTF-8 compliance, reject null bytes / unescaped ASCII control codes, and caps size at `MAX_TEXT_BYTES = 1_000_000`.

### Structured Evaluation Outcomes (`InputOutcome`)
1. `ACCEPTED`: Valid research note text with assigned provenance. Downstream ingestion proceeds (`can_proceed=True`).
2. `REJECTED`: Structural invalidity, repeated character spam, punctuation noise, or fragment noise. Downstream processing halted; audited into `failed/`.
3. `NEEDS_CLARIFICATION`: Ambiguous single-token inputs (e.g. "a", "x", "1") requiring additional researcher context without presumption of intent.
4. `STOPPED`: Cooperative cancellation requested by researcher; stops pipeline cleanly and audits `stopped_by_user`.
5. `ERROR`: Unexpected execution error or backend unavailability.

---

## 4. Deterministic Noise Boundaries vs. Research Notes

To protect against accidental input corruption while respecting authentic research syntax:

1. **Punctuation & Symbol Noise (`_is_punctuation_or_symbol_only`):**
   * Rejects inputs containing exclusively punctuation, whitespace, or symbols without letters or numbers.
2. **Repeated Character Spam (`_is_repeated_char_spam`):**
   * Rejects sequences dominated by single repeating characters (e.g., `aaaaaaaaaa`, `سسسسسسسسسسس`, `111111111111`).
3. **Repeated Fragment Noise (`_is_repeated_fragment_noise`):**
   * Detects repeated keyboard-mashing patterns of length 2 to 8 repeating $\ge 3$ times and covering $\ge 80\%$ of text (e.g., `qwertyqwertyqwerty`, `asdfasdfasdfasdf`).
4. **Legitimate Short & Multilingual Notes Preserved:**
   * Checks leverage Unicode category testing (`unicodedata.category(ch)[0] in ('L', 'N')`).
   * Legitimate brief research queries, formulas, and multilingual expressions (e.g., `"Why?"`, `"چرا؟"`, `"AI-001"`, `"x = y + 1"`, code snippets) are classified as valid research notes and accepted.

---

## 5. In-UI Actionable Recovery Workflow

When an input is rejected, stopped, requires clarification, or fails connection:

* **Location:** Rendered prominently inside `ProcessingResult`.
* **Status Badges:** Outcome (`[REJECTED]`, `[NEEDS CLARIFICATION]`, `[STOPPED]`, `[ERROR]`), error code (`<code>`), and `run_id`.
* **Draft Display:** Renders a blockquote preview of the submitted draft (`state.lastSubmittedText`).
* **Recovery Actions:**
  * **Fix / Edit Input (`fixInput`):** Transfers the preserved draft back into the composer and focuses the textarea for immediate editing without terminal use.
  * **Retry (`retryLastInput`):** Re-submits the preserved draft to the backend.
  * **Stop / Dismiss (`dismissRunOutcome`):** Resets the run state cleanly back to idle.
* **In-Flight Cancellation:** Active runs can be stopped cooperatively at any point from `ProcessingResult` and `TopNav`, triggering backend cancellation and recording `stopped_by_user`.

---

## 6. Privacy & Audit Governance

* **Zero Raw Text Leakage:** Raw private input text is strictly excluded from persistent audit logs (`failed/audit.jsonl`).
* **Audit Metadata:** Audit entries persist only non-sensitive provenance:
  * `input_id`, `input_hash`, `outcome`, `error_code`, `byte_length`, `char_length`, `timestamp`, and `terminal_outcome` (`input_validation_failed`, `input_clarification_required`, or `stopped_by_user`).
* **Storage Isolation:** All validation failures and stopped runs are partitioned into the `failed/` storage root and never touch staging or vault storage.

