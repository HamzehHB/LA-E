import { useState } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";

function validityKey(v: string): string {
  switch (v) {
    case "configured_valid":
      return "ui.state.configured";
    case "configured_invalid":
      return "ui.state.invalid";
    case "configured_unavailable":
      return "ui.state.unavailable";
    case "validation_pending":
      return "ui.state.pending";
    default:
      return "ui.state.notConfigured";
  }
}

function validityClass(v: string): string {
  switch (v) {
    case "configured_valid":
      return "ok";
    case "configured_invalid":
    case "configured_unavailable":
    case "validation_error":
      return "bad";
    case "validation_pending":
      return "warn";
    default:
      return "";
  }
}

export function TopNav() {
  const { state, setSourceMode, setRetrievalType, setLlmMode, setStagingLanguage, setAuditLanguage, liveAvailable, pickLiveFolder } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (k: string) => setOpen((v) => (v === k ? null : k));

  const onSourceClick = () => {
    const next = state.sourceMode === "test" ? "vault" : "test";
    setSourceMode(next);
    // Selecting Vault validates a vault path through the picker; it does
    // NOT scan, ingest, or index. Test never touches the vault.
    if (next === "vault" && liveAvailable) void pickLiveFolder("vault");
  };
  // Semantic retrieval is only genuinely available when both an isolated
  // vector database and an embedding model have been backend-validated
  // this session. Otherwise the UI says so and never claims it is active.
  const semanticReady =
    state.resources.vector_db.validity === "configured_valid" &&
    state.resources.embedding_model.validity === "configured_valid";
  // §3 mode tab treatment: Test is red; Vault is green only when valid.
  const vaultValid = state.resources.vault.validity === "configured_valid";
  const modeClass = state.sourceMode === "test" ? "mode-test" : vaultValid ? "mode-vault mode-ready" : "mode-vault";

  return (
    <nav aria-label="top" className="topnav">
      <button
        type="button"
        className={modeClass}
        aria-pressed={state.sourceMode === "vault"}
        onClick={onSourceClick}
        title={state.sourceMode === "vault" ? t("ui.nav.vaultWarning") : t("ui.nav.testHint")}
      >
        {state.sourceMode === "test" ? t("ui.nav.sourceTest") : t("ui.nav.sourceVault")} ▾
        <span className={`badge ${validityClass(state.resources.vault.validity)}`} aria-label={t(validityKey(state.resources.vault.validity) as Parameters<typeof translate>[1])}>
          {t(validityKey(state.resources.vault.validity) as Parameters<typeof translate>[1])}
        </span>
      </button>
      <button type="button" onClick={() => { if (liveAvailable) void pickLiveFolder("staging"); }} title={state.resources.staging.reason || undefined}>
        {t("ui.nav.staging")} <span className={`badge ${validityClass(state.resources.staging.validity)}`}>{t(validityKey(state.resources.staging.validity) as Parameters<typeof translate>[1])}</span>
      </button>
      <button type="button" onClick={() => { if (liveAvailable) void pickLiveFolder("audit"); }} title={state.resources.audit.reason || undefined}>
        {t("ui.nav.audit")} <span className={`badge ${validityClass(state.resources.audit.validity)}`}>{t(validityKey(state.resources.audit.validity) as Parameters<typeof translate>[1])}</span>
      </button>
      <div className="menu">
        <button type="button" aria-expanded={open === "db"} onClick={() => toggle("db")}>
          {t("ui.nav.database")} ▾
        </button>
        {open === "db" && (
          <div role="menu" className="dropdown">
            <span className="note">{t("ui.db.location")}</span>
            <button type="button" role="menuitem" onClick={() => { if (liveAvailable) void pickLiveFolder("vector_db"); }}>
              {t("ui.nav.databaseLocation")} · <span className={`badge ${validityClass(state.resources.vector_db.validity)}`}>{t(validityKey(state.resources.vector_db.validity) as Parameters<typeof translate>[1])}</span>
            </button>
            <hr />
            <span className="note">{t("ui.db.retrievalType")}</span>
            <button type="button" role="menuitemradio" aria-checked={state.retrievalType === "lexical"} onClick={() => setRetrievalType("lexical")}>
              Lexical Retrieval
            </button>
            <button
              type="button"
              role="menuitemradio"
              aria-checked={state.retrievalType === "semantic"}
              onClick={() => setRetrievalType("semantic")}
              title={semanticReady ? undefined : t("ui.nav.semanticNeedsSetup")}
            >
              Semantic Retrieval{semanticReady ? "" : " ⚠"}
            </button>
            {!semanticReady && (
              <span className="note">{t("ui.nav.semanticNeedsSetup")}</span>
            )}
          </div>
        )}
      </div>
      <div className="menu">
        <button type="button" aria-expanded={open === "ai"} onClick={() => toggle("ai")}>
          {t("ui.nav.ai")} ▾
        </button>
        {open === "ai" && (
          <div role="menu" className="dropdown">
            <span className="note">LLM</span>
            <button type="button" role="menuitemradio" aria-checked={state.llmMode === "local"} onClick={() => setLlmMode("local")}>
              {t("ui.ai.localLlm")}
            </button>
            <button type="button" role="menuitemradio" aria-checked={state.llmMode === "cloud"} onClick={() => setLlmMode("cloud")}>
              {t("ui.ai.cloudLlm")}
            </button>
            {state.llmMode === "local" ? (
              <>
                <hr />
                <span className="note">{t("ui.ai.modelLocation")}</span>
                <button type="button" role="menuitem" onClick={() => { if (liveAvailable) void pickLiveFolder("local_llm"); }}>
                  {t("ui.ai.modelLocation")} · <span className={`badge ${validityClass(state.resources.local_llm.validity)}`}>{t(validityKey(state.resources.local_llm.validity) as Parameters<typeof translate>[1])}</span>
                </button>
                <button type="button" role="menuitem" onClick={() => { if (liveAvailable) void pickLiveFolder("embedding_model"); }}>
                  {t("ui.ai.embeddingLocation")} · <span className={`badge ${validityClass(state.resources.embedding_model.validity)}`}>{t(validityKey(state.resources.embedding_model.validity) as Parameters<typeof translate>[1])}</span>
                </button>
                <span role="note" className="note">{t("ui.ai.localLaunch")}</span>
              </>
            ) : (
              <>
                <hr />
                <span role="note" className="note">{t("ui.ai.cloudUnsupported")}</span>
              </>
            )}
          </div>
        )}
      </div>
      <div className="menu">
        <button type="button" aria-expanded={open === "lang"} onClick={() => toggle("lang")}>
          {t("ui.nav.language")} ▾
        </button>
        {open === "lang" && (
          <div role="menu" className="dropdown">
            <span className="note">Audit language</span>
            <button type="button" role="menuitemradio" aria-checked={state.auditLanguage === "en"} onClick={() => setAuditLanguage("en")}>English</button>
            <button type="button" role="menuitemradio" aria-checked={state.auditLanguage === "fa"} onClick={() => setAuditLanguage("fa")}>فارسی</button>
            <span className="note">Staging language</span>
            <button type="button" role="menuitemradio" aria-checked={state.stagingLanguage === "en"} onClick={() => setStagingLanguage("en")}>English</button>
            <button type="button" role="menuitemradio" aria-checked={state.stagingLanguage === "fa"} onClick={() => setStagingLanguage("fa")}>فارسی</button>
            <span role="note">Content languages for generated Audit and Staging text; independent of App language.</span>
            <span role="note" className="note">{t("ui.lang.contentNote")}</span>
          </div>
        )}
      </div>
    </nav>
  );
}
