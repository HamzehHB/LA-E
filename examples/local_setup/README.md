# Local Setup — Operator Walkthrough

A transferable, synthetic walkthrough of the current single-input
Knowledge Management loop. Replace every `<placeholder>` with your own
value. No real path, credential, or note content belongs in this file.

## 1. Environment

```powershell
python -m venv Venv
Venv\Scripts\activate
pip install -r requirements.txt
```

The virtual environment is the runtime. `Venv\Scripts\python -m ...` is
the portable alternative when activation is inconvenient; system Python
is not the project runtime.

## 2. Local configuration

`Config/paths.local.yaml` (copy from [`paths.example.yaml`](../../Config/paths.example.yaml)):

```yaml
data:
  root: "<YOUR_DATA_ROOT>"
models:
  bge_m3: "<YOUR_DATA_ROOT>/Models/Embedding_Model/BGE_M3"
vector_db:
  lancedb: "<YOUR_DATA_ROOT>/Database/Vector_Database"
memory:
  root: "<YOUR_DATA_ROOT>/Memory"
obsidian:
  vault: "<YOUR_VAULT_ROOT>"
zotero:
  library: "<YOUR_DATA_ROOT>/Knowledge/Zotero"
exports:
  root: "<YOUR_DATA_ROOT>/Knowledge/Exports"
cache:
  root: "<YOUR_DATA_ROOT>/Database/Cache"
staging:
  root: "<YOUR_DATA_ROOT>/Knowledge/Staging"
```

`staging.root` is the single authorized staging destination and is still
validated by the unchanged guard (explicit, existing, real directory, not
a symlink, outside the repository, resolving identically to this key).

`Config/llm.local.yaml` (copy from [`llm.example.yaml`](../../Config/llm.example.yaml); gitignored):

```yaml
llm:
  provider: "ollama"
  model: "<YOUR_OLLAMA_MODEL>"
  endpoint: "http://127.0.0.1:11434"
  timeout_seconds: 120
  max_tokens: 1024
```

Cloud credentials live in the environment variable named by
`credential_env`, never in YAML.

## 3. One input

```powershell
python -m src.living_authenticity.cli --input-file "<YOUR_NOTE.md>"
python -m src.living_authenticity.cli --text "<YOUR_TEXT>"
```

Optional read-only vault context: add `--vault-context`.
Verbose intermediates: add `--verbose`.

## 4. What happens

`ingestion -> retrieval -> comparison -> relation -> core -> classification
-> proposal -> confidence -> knowledge filter -> LLM synthesis (mandatory)
-> human review -> explicit approval -> revalidation -> controlled
execution -> audit`

LLM synthesis is a formal, mandatory stage. If the configured provider is
disabled, unreachable, times out, or returns unusable output, the affected
unit stops safely: no candidate is displayed, no approval is asked, no
execution happens, and the run reports the reason.

## 5. What is not automatic

Approval is per candidate and defaults to No. `CREATE` writes one artifact
into the staging area only — never into the vault. Placing an approved note
into the authoritative vault stays a human action.

## 6. Adding a different cloud API shape

The provider layer is provider-independent: add one small module under
`src/living_authenticity/llm/` implementing `LLMProvider`
(`name`, `model`, `endpoint`, `complete`) plus one dispatch branch in
`registry.build_provider`. The shared interface and existing provider
modules are never modified, and every cloud shape is subject to the same
resolved-endpoint privacy gate. Use stdlib `urllib` — the layer adds no
dependency.
