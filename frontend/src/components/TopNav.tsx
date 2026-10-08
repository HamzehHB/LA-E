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

export function TopNav() {
  const { state, setSourceMode, setRetrievalType, setLlmMode } = useStore();
  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (k: string) => setOpen((v) => (v === k ? null : k));

  return (
    <nav aria-label="top" className="topnav">
      <button type="button" aria-label={t("ui.nav.settings")}>
        ⚙ {t("ui.nav.settings")}
      </button>
      <button
        type="button"
        aria-pressed={state.sourceMode === "vault"}
        onClick={() => setSourceMode(state.sourceMode === "test" ? "vault" : "test")}
      >
        {state.sourceMode === "test" ? t("ui.nav.sourceTest") : t("ui.nav.sourceVault")} ▾
        <span className="badge" aria-label={t(validityKey(state.resources.vault.validity) as Parameters<typeof translate>[1])}>
          {t(validityKey(state.resources.vault.validity) as Parameters<typeof translate>[1])}
        </span>
      </button>
      <button type="button">
        {t("ui.nav.staging")} <span className="badge">{t(validityKey(state.resources.staging.validity) as Parameters<typeof translate>[1])}</span>
      </button>
      <button type="button">
        {t("ui.nav.audit")} <span className="badge">{t(validityKey(state.resources.audit.validity) as Parameters<typeof translate>[1])}</span>
      </button>
      <div className="menu">
        <button type="button" aria-expanded={open === "db"} onClick={() => toggle("db")}>
          {t("ui.nav.database")} ▾
        </button>
        {open === "db" && (
          <div role="menu" className="dropdown">
            <button type="button" role="menuitemradio" aria-checked={state.retrievalType === "lexical"} onClick={() => setRetrievalType("lexical")}>
              Lexical Retrieval
            </button>
            <button type="button" role="menuitemradio" aria-checked={state.retrievalType === "semantic"} onClick={() => setRetrievalType("semantic")}>
              Semantic Retrieval
            </button>
          </div>
        )}
      </div>
      <div className="menu">
        <button type="button" aria-expanded={open === "ai"} onClick={() => toggle("ai")}>
          {t("ui.nav.ai")} ▾
        </button>
        {open === "ai" && (
          <div role="menu" className="dropdown">
            <button type="button" role="menuitemradio" aria-checked={state.llmMode === "local"} onClick={() => setLlmMode("local")}>
              LLM: Local
            </button>
            <button type="button" role="menuitemradio" aria-checked={state.llmMode === "cloud"} onClick={() => setLlmMode("cloud")}>
              LLM: Cloud
            </button>
          </div>
        )}
      </div>
      <div className="menu">
        <button type="button" aria-expanded={open === "lang"} onClick={() => toggle("lang")}>
          {t("ui.nav.language")} ▾
        </button>
        {open === "lang" && (
          <div role="menu" className="dropdown">
            <span role="note">App / Staging / Audit languages are independent (en/fa).</span>
          </div>
        )}
      </div>
    </nav>
  );
}
