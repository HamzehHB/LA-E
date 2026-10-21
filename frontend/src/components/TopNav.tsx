import { useState, useEffect } from "react";
import { useStore } from "../state/store";
import { translate } from "../i18n/resources";
import type { FolderPurpose, VaultSyncStatusResponse } from "../contracts/transport";

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
  const {
    state,
    setTheme,
    setAppLanguage,
    setSourceMode,
    setRetrievalType,
    setLlmMode,
    setStagingLanguage,
    setAuditLanguage,
    liveAvailable,
    liveConnected,
    pickLiveFolder,
    liveAuthorizeVaultSync,
    liveRevokeVaultSync,
    liveReconcileVaultSync,
    liveGetVaultSyncStatus,
    liveCancel,
    liveRevealFolder,
  } = useStore();

  const t = (k: Parameters<typeof translate>[1]) => translate(state.appLanguage, k);
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (k: string) => setOpen((v) => (v === k ? null : k));
  const [syncStatus, setSyncStatus] = useState<VaultSyncStatusResponse | null>(null);

  useEffect(() => {
    if (liveAvailable && open === "db") {
      void liveGetVaultSyncStatus().then(setSyncStatus);
    }
  }, [open, liveAvailable, liveGetVaultSyncStatus]);

  // Contextual first-click onboarding states
  const [vaultModalOpen, setVaultModalOpen] = useState(false);
  const [stagingModalOpen, setStagingModalOpen] = useState(false);
  const [auditModalOpen, setAuditModalOpen] = useState(false);
  const [dbModalOpen, setDbModalOpen] = useState(false);
  const [indexingMsg, setIndexingMsg] = useState("");
  const [inspectModal, setInspectModal] = useState<{
    purpose: FolderPurpose;
    title: string;
    items: Array<{ name: string; is_dir: boolean; size: number; modified: string }>;
  } | null>(null);

  const semanticReady =
    state.resources.vector_db.validity === "configured_valid" &&
    state.resources.embedding_model.validity === "configured_valid";

  const vaultValid = state.resources.vault.validity === "configured_valid";
  const stagingValid = state.resources.staging.validity === "configured_valid";
  const auditValid = state.resources.audit.validity === "configured_valid";

  const isFixtureRun = Boolean(state.run.run_id && state.run.run_id.startsWith("run-fixture"));
  const runActive =
    liveAvailable &&
    Boolean(state.run.run_id) &&
    !isFixtureRun &&
    !state.run.stopped &&
    Boolean(state.run.status) &&
    !state.run.status.toLowerCase().includes("finished") &&
    !state.run.status.toLowerCase().includes("terminal") &&
    !state.run.status.toLowerCase().includes("completed") &&
    !state.run.status.toLowerCase().includes("already completed") &&
    state.run.current_stage !== "completed";

  const inspectFolder = async (purpose: FolderPurpose, title: string) => {
    if (!liveAvailable) return;
    const items = await liveRevealFolder(purpose);
    setInspectModal({ purpose, title, items });
  };

  // Vault / Test Mode Click Handler
  const handleSourceTabClick = () => {
    if (state.sourceMode === "test") {
      // Transition from Test -> Vault
      setSourceMode("vault");
      const hasSeenVault = localStorage.getItem("lae-first-vault");
      if (!hasSeenVault) {
        localStorage.setItem("lae-first-vault", "1");
        setVaultModalOpen(true);
      } else if (!vaultValid && liveAvailable) {
        void pickLiveFolder("vault");
      }
    } else {
      // Transition from Vault -> Test
      setSourceMode("test");
    }
  };

  // Staging Click Handler
  const handleStagingClick = () => {
    const hasSeen = localStorage.getItem("lae-first-staging");
    if (!hasSeen) {
      localStorage.setItem("lae-first-staging", "1");
      setStagingModalOpen(true);
    } else if (stagingValid && liveAvailable) {
      void inspectFolder("staging", t("ui.nav.staging"));
    } else if (liveAvailable) {
      void pickLiveFolder("staging");
    }
  };

  // Audits Click Handler
  const handleAuditsClick = () => {
    const hasSeen = localStorage.getItem("lae-first-audit");
    if (!hasSeen) {
      localStorage.setItem("lae-first-audit", "1");
      setAuditModalOpen(true);
    } else if (auditValid && liveAvailable) {
      void inspectFolder("audit", t("ui.nav.audit"));
    } else if (liveAvailable) {
      void pickLiveFolder("audit");
    }
  };

  // Database Location Click Handler
  const handleDbLocationClick = () => {
    const hasSeen = localStorage.getItem("lae-first-db");
    if (!hasSeen) {
      localStorage.setItem("lae-first-db", "1");
      setDbModalOpen(true);
    } else if (liveAvailable) {
      void pickLiveFolder("vector_db");
    }
  };

  const runInitialIndex = async () => {
    setIndexingMsg("Running initial vector indexing…");
    const res = await liveAuthorizeVaultSync();
    if (res.indexed) {
      setIndexingMsg("Initial index complete!");
      if (liveAvailable) {
        void liveGetVaultSyncStatus().then(setSyncStatus);
      }
      setTimeout(() => {
        setVaultModalOpen(false);
        setIndexingMsg("");
      }, 1500);
    } else {
      setIndexingMsg(res.reason || "Indexing failed. Verify Database and Embedding model settings.");
    }
  };

  return (
    <nav aria-label="top" className="topnav-bar">
      {/* 1. Left Cluster: Brand + Settings Gear + Live Status */}
      <div className="brand-cluster">
        <div className="brand">
          <span className="logo" aria-label="LA-E">
            <span className="logo-l" aria-hidden="true">L</span>
            <span className="logo-a" aria-hidden="true">A</span>
            <span className="logo-sep" aria-hidden="true">-</span>
            <span className="logo-e" aria-hidden="true">E</span>
          </span>
          <span className="ver">v0.1</span>
        </div>

        <button
          type="button"
          className="gear-btn"
          aria-label={t("ui.nav.settings")}
          aria-expanded={open === "settings"}
          onClick={() => toggle("settings")}
        >
          ⚙
        </button>

        {open === "settings" && (
          <div role="menu" className="dropdown settings-popover">
            <span className="dropdown-title">{t("ui.settings.title")}</span>
            <div className="dropdown-section">
              <span className="note">{t("ui.settings.display")}</span>
              <div role="radiogroup" aria-label={t("ui.settings.display")} className="review">
                <button
                  type="button"
                  role="radio"
                  className={state.theme === "light" ? "active" : ""}
                  aria-checked={state.theme === "light"}
                  aria-label={t("ui.settings.light")}
                  onClick={() => setTheme("light")}
                >
                  ☀ {t("ui.settings.light")}
                </button>
                <button
                  type="button"
                  role="radio"
                  className={state.theme === "dark" ? "active" : ""}
                  aria-checked={state.theme === "dark"}
                  aria-label={t("ui.settings.dark")}
                  onClick={() => setTheme("dark")}
                >
                  🌙 {t("ui.settings.dark")}
                </button>
              </div>
            </div>
            <hr />
            <div className="dropdown-section">
              <span className="note">{t("ui.settings.appLanguage")}</span>
              <div role="radiogroup" aria-label={t("ui.settings.appLanguage")} className="review">
                <button
                  type="button"
                  role="radio"
                  className={state.appLanguage === "en" ? "active" : ""}
                  aria-checked={state.appLanguage === "en"}
                  aria-label="English"
                  onClick={() => setAppLanguage("en")}
                >
                  English
                </button>
                <button
                  type="button"
                  role="radio"
                  className={state.appLanguage === "fa" ? "active" : ""}
                  aria-checked={state.appLanguage === "fa"}
                  aria-label="فارسی"
                  onClick={() => setAppLanguage("fa")}
                >
                  فارسی
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="live-status-pill" title={liveAvailable ? (liveConnected ? "Live loopback connected" : "Connecting to loopback…") : "Fixture demo mode"}>
          <span className={`live-dot ${liveAvailable ? (liveConnected ? "on" : "off") : ""}`} aria-hidden="true" />
          <span className="live-text">
            {liveAvailable
              ? (liveConnected ? t("ui.live.connected") : "reconnecting…")
              : t("ui.live.demo")}
          </span>
        </div>

        {runActive && (
          <button
            type="button"
            className="btn small danger cancel-run-btn"
            title={t("ui.run.cancelTitle")}
            onClick={() => void liveCancel()}
          >
            ⏹ {t("ui.run.cancel")}
          </button>
        )}
      </div>

      {/* 2. Center-Left Cluster: The 3 Status Pill Tabs */}
      <div className="status-tabs-cluster">
        {/* TEST / VAULT Tab */}
        <button
          type="button"
          className={`pill-tab ${state.sourceMode === "test" ? "tab-test" : (vaultValid ? "tab-vault ok" : "tab-vault bad")}`}
          aria-pressed={state.sourceMode === "vault"}
          onClick={handleSourceTabClick}
          title={state.sourceMode === "vault" ? t("ui.nav.vaultWarning") : t("ui.nav.testHint")}
        >
          <span className="tab-label">{state.sourceMode === "test" ? t("ui.nav.sourceTest") : t("ui.nav.sourceVault")}</span>
          {state.sourceMode === "vault" && (
            <span className={`badge ${validityClass(state.resources.vault.validity)}`}>
              {state.resources.vault.display || t(validityKey(state.resources.vault.validity) as Parameters<typeof translate>[1])}
            </span>
          )}
        </button>

        {/* STAGING Tab */}
        <button
          type="button"
          className={`pill-tab ${stagingValid ? "tab-staging ok" : "tab-staging bad"}`}
          onClick={handleStagingClick}
          title={state.resources.staging.reason || undefined}
        >
          <span className="tab-label">{t("ui.nav.staging")}</span>
          <span className={`badge ${validityClass(state.resources.staging.validity)}`}>
            {state.resources.staging.display || t(validityKey(state.resources.staging.validity) as Parameters<typeof translate>[1])}
          </span>
        </button>

        {/* AUDITS Tab */}
        <button
          type="button"
          className={`pill-tab ${auditValid ? "tab-audits ok" : "tab-audits bad"}`}
          onClick={handleAuditsClick}
          title={state.resources.audit.reason || undefined}
        >
          <span className="tab-label">{t("ui.nav.audit")}</span>
          <span className={`badge ${validityClass(state.resources.audit.validity)}`}>
            {state.resources.audit.display || t(validityKey(state.resources.audit.validity) as Parameters<typeof translate>[1])}
          </span>
        </button>
      </div>

      {/* 3. Right Cluster: The 3 Compact Dropdown Menus */}
      <div className="dropdowns-cluster">
        {/* Database Menu */}
        <div className="menu">
          <button type="button" className="menu-btn" aria-expanded={open === "db"} onClick={() => toggle("db")}>
            {t("ui.nav.database")} ▾
          </button>
          {open === "db" && (
            <div role="menu" className="dropdown">
              <span className="dropdown-title">{t("ui.db.location")}</span>
              <button type="button" role="menuitem" onClick={handleDbLocationClick}>
                {t("ui.nav.databaseLocation")} ·{" "}
                <span className={`badge ${validityClass(state.resources.vector_db.validity)}`}>
                  {state.resources.vector_db.display || t(validityKey(state.resources.vector_db.validity) as Parameters<typeof translate>[1])}
                </span>
              </button>
              <hr />
              <span className="dropdown-title">Vault Index Sync</span>
              {syncStatus && (
                <div style={{ padding: "0.25rem 0.5rem", fontSize: "0.85em" }}>
                  <span>Status: <strong>{syncStatus.status}</strong></span>
                  {syncStatus.indexed_notes > 0 && <span> ({syncStatus.indexed_notes} notes, {syncStatus.indexed_vectors} vectors)</span>}
                </div>
              )}
              <div style={{ display: "flex", gap: "0.5rem", padding: "0.25rem 0.5rem" }}>
                {syncStatus?.authorized ? (
                  <>
                    <button
                      type="button"
                      className="btn small"
                      onClick={async () => {
                        await liveReconcileVaultSync();
                        const s = await liveGetVaultSyncStatus();
                        setSyncStatus(s);
                      }}
                    >
                      Re-sync Now
                    </button>
                    <button
                      type="button"
                      className="btn small danger"
                      onClick={async () => {
                        await liveRevokeVaultSync();
                        const s = await liveGetVaultSyncStatus();
                        setSyncStatus(s);
                      }}
                    >
                      Revoke Sync
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="btn small"
                    disabled={!semanticReady}
                    onClick={async () => {
                      await liveAuthorizeVaultSync();
                      const s = await liveGetVaultSyncStatus();
                      setSyncStatus(s);
                    }}
                  >
                    Authorize Sync
                  </button>
                )}
              </div>
              <hr />
              <span className="dropdown-title">{t("ui.strategy.title")}</span>
              <button
                type="button"
                role="menuitemradio"
                className={state.retrievalType === "lexical" ? "selected" : ""}
                aria-checked={state.retrievalType === "lexical"}
                onClick={() => setRetrievalType("lexical")}
              >
                ● {t("ui.strategy.lexical")}
              </button>
              <button
                type="button"
                role="menuitemradio"
                className={state.retrievalType === "semantic" ? "selected" : ""}
                aria-checked={state.retrievalType === "semantic"}
                onClick={() => setRetrievalType("semantic")}
                title={semanticReady ? undefined : t("ui.nav.semanticNeedsSetup")}
              >
                ● {t("ui.strategy.semantic")} {semanticReady ? "" : "⚠"}
              </button>
              <button
                type="button"
                role="menuitemradio"
                className="disabled"
                disabled={true}
                aria-checked={false}
                title={t("ui.strategy.hybridDisabled")}
              >
                ○ {t("ui.strategy.hybrid")}
              </button>
              {!semanticReady && (
                <span className="note warn">{t("ui.nav.semanticNeedsSetup")}</span>
              )}
            </div>
          )}
        </div>

        {/* AI Menu */}
        <div className="menu">
          <button type="button" className="menu-btn" aria-expanded={open === "ai"} onClick={() => toggle("ai")}>
            {t("ui.nav.ai")} ▾
          </button>
          {open === "ai" && (
            <div role="menu" className="dropdown">
              <span className="dropdown-title">Embedding Model</span>
              <button type="button" role="menuitem" onClick={() => { if (liveAvailable) void pickLiveFolder("embedding_model"); }}>
                BGE-M3 ·{" "}
                <span className={`badge ${validityClass(state.resources.embedding_model.validity)}`}>
                  {state.resources.embedding_model.display || t(validityKey(state.resources.embedding_model.validity) as Parameters<typeof translate>[1])}
                </span>
              </button>
              <a
                href="https://huggingface.co/BAAI/bge-m3"
                target="_blank"
                rel="noreferrer"
                className="dropdown-link"
              >
                Download BGE-M3 on Hugging Face ↗
              </a>
              <hr />
              <span className="dropdown-title">LLM</span>
              <button
                type="button"
                role="menuitemradio"
                className={state.llmMode === "local" ? "selected" : ""}
                aria-checked={state.llmMode === "local"}
                onClick={() => setLlmMode("local")}
              >
                ● {t("ui.ai.localLlm")}
              </button>
              <button
                type="button"
                role="menuitemradio"
                className={state.llmMode === "cloud" ? "selected" : ""}
                aria-checked={state.llmMode === "cloud"}
                onClick={() => setLlmMode("cloud")}
              >
                ● {t("ui.ai.cloudLlm")}
              </button>
              {state.llmMode === "local" ? (
                <>
                  <button type="button" role="menuitem" onClick={() => { if (liveAvailable) void pickLiveFolder("local_llm"); }}>
                    {t("ui.ai.modelLocation")} ·{" "}
                    <span className={`badge ${validityClass(state.resources.local_llm.validity)}`}>
                      {state.resources.local_llm.display || t(validityKey(state.resources.local_llm.validity) as Parameters<typeof translate>[1])}
                    </span>
                  </button>
                  <a
                    href="https://huggingface.co/models?search=gguf"
                    target="_blank"
                    rel="noreferrer"
                    className="dropdown-link"
                  >
                    Download GGUF Models ↗
                  </a>
                </>
              ) : (
                <span role="note" className="note">{t("ui.ai.cloudUnsupported")}</span>
              )}
            </div>
          )}
        </div>

        {/* Knowledge Language Menu */}
        <div className="menu">
          <button type="button" className="menu-btn" aria-expanded={open === "lang"} onClick={() => toggle("lang")}>
            {t("ui.nav.language")} ▾
          </button>
          {open === "lang" && (
            <div role="menu" className="dropdown">
              <span className="dropdown-title">Audit Language</span>
              <div className="button-group">
                <button
                  type="button"
                  role="menuitemradio"
                  className={state.auditLanguage === "en" ? "selected" : ""}
                  aria-checked={state.auditLanguage === "en"}
                  onClick={() => setAuditLanguage("en")}
                >
                  English
                </button>
                <button
                  type="button"
                  role="menuitemradio"
                  className={state.auditLanguage === "fa" ? "selected" : ""}
                  aria-checked={state.auditLanguage === "fa"}
                  onClick={() => setAuditLanguage("fa")}
                >
                  فارسی
                </button>
              </div>
              <hr />
              <span className="dropdown-title">Staging Language</span>
              <div className="button-group">
                <button
                  type="button"
                  role="menuitemradio"
                  className={state.stagingLanguage === "en" ? "selected" : ""}
                  aria-checked={state.stagingLanguage === "en"}
                  onClick={() => setStagingLanguage("en")}
                >
                  English
                </button>
                <button
                  type="button"
                  role="menuitemradio"
                  className={state.stagingLanguage === "fa" ? "selected" : ""}
                  aria-checked={state.stagingLanguage === "fa"}
                  onClick={() => setStagingLanguage("fa")}
                >
                  فارسی
                </button>
              </div>
              <span role="note" className="note">{t("ui.lang.contentNote")}</span>
            </div>
          )}
        </div>
      </div>

      {/* Contextual Modal: First-Time Vault Onboarding */}
      {vaultModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="vault-modal-title">
          <div className="modal-card">
            <h3 id="vault-modal-title">Initial Vault Vector Indexing</h3>
            <p>For a more personalized experience with your data, an initial vector index is needed. Would you like to create the first one?</p>
            <div className="modal-checks">
              <div className="check-item">
                <span>Database Location:</span>
                <span className={`badge ${validityClass(state.resources.vector_db.validity)}`}>
                  {state.resources.vector_db.display || "Not configured"}
                </span>
                {state.resources.vector_db.validity !== "configured_valid" && (
                  <button type="button" className="btn small" onClick={() => void pickLiveFolder("vector_db")}>Select Folder</button>
                )}
              </div>
              <div className="check-item">
                <span>Embedding Model (BGE-M3):</span>
                <span className={`badge ${validityClass(state.resources.embedding_model.validity)}`}>
                  {state.resources.embedding_model.display || "Not configured"}
                </span>
                {state.resources.embedding_model.validity !== "configured_valid" && (
                  <button type="button" className="btn small" onClick={() => void pickLiveFolder("embedding_model")}>Choose Model</button>
                )}
              </div>
            </div>
            {indexingMsg && <p className="status-msg">{indexingMsg}</p>}
            <div className="modal-actions">
              <button
                type="button"
                className="btn approve"
                disabled={!semanticReady}
                onClick={() => void runInitialIndex()}
              >
                Create Initial Index
              </button>
              <a
                href="https://huggingface.co/BAAI/bge-m3"
                target="_blank"
                rel="noreferrer"
                className="btn ghost"
              >
                Download BGE-M3 ↗
              </a>
              <button type="button" className="btn ghost" onClick={() => setVaultModalOpen(false)}>Not now</button>
            </div>
          </div>
        </div>
      )}

      {/* Contextual Modal: First-Time Staging */}
      {stagingModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="staging-modal-title">
          <div className="modal-card">
            <h3 id="staging-modal-title">Staging Destination</h3>
            <p>Selecting a Staging folder provides a safe, isolated, and controlled destination for approved note outputs before they ever touch your authoritative vault.</p>
            <div className="modal-actions">
              {stagingValid && liveAvailable && (
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => {
                    setStagingModalOpen(false);
                    void inspectFolder("staging", t("ui.nav.staging"));
                  }}
                >
                  {t("ui.nav.inspectFolder")}
                </button>
              )}
              <button
                type="button"
                className="btn approve"
                onClick={() => {
                  setStagingModalOpen(false);
                  if (liveAvailable) void pickLiveFolder("staging");
                }}
              >
                Select Staging Folder
              </button>
              <button type="button" className="btn ghost" onClick={() => setStagingModalOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Contextual Modal: First-Time Audits */}
      {auditModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="audit-modal-title">
          <div className="modal-card">
            <h3 id="audit-modal-title">Audit Trail Destination</h3>
            <p>Audits store append-only, tamper-evident traceability records. Configuring an Audit folder ensures <strong>passed</strong>, <strong>failed</strong>, and <strong>errors</strong> subdirectories are created and kept strictly disjoint from your knowledge vault.</p>
            <div className="modal-actions">
              {auditValid && liveAvailable && (
                <button
                  type="button"
                  className="btn ghost"
                  onClick={() => {
                    setAuditModalOpen(false);
                    void inspectFolder("audit", t("ui.nav.audit"));
                  }}
                >
                  {t("ui.nav.inspectFolder")}
                </button>
              )}
              <button
                type="button"
                className="btn approve"
                onClick={() => {
                  setAuditModalOpen(false);
                  if (liveAvailable) void pickLiveFolder("audit");
                }}
              >
                Select Audit Folder
              </button>
              <button type="button" className="btn ghost" onClick={() => setAuditModalOpen(false)}>Cancel</button>
            </div>
          </div>
        </div>
      )}

      {/* Contextual Modal: First-Time Database */}
      {dbModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="db-modal-title">
          <div className="modal-card">
            <h3 id="db-modal-title">Vector Database Engine</h3>
            <p>This feature requires a local vector database engine. LanceDB is the recommended isolated engine for high-efficiency semantic retrieval.</p>
            <div className="modal-actions">
              <button
                type="button"
                className="btn approve"
                onClick={() => {
                  setDbModalOpen(false);
                  if (liveAvailable) void pickLiveFolder("vector_db");
                }}
              >
                Configure LanceDB Folder
              </button>
              <button type="button" className="btn ghost" onClick={() => setDbModalOpen(false)}>Not now</button>
            </div>
          </div>
        </div>
      )}

      {/* Inspect Folder Modal */}
      {inspectModal && (
        <div className="modal-overlay" role="dialog" aria-modal="true" aria-labelledby="inspect-modal-title">
          <div className="modal-card" style={{ maxWidth: "600px" }}>
            <h3 id="inspect-modal-title">{inspectModal.title} — {t("ui.nav.folderItems")}</h3>
            <div style={{ maxHeight: "300px", overflowY: "auto", margin: "1rem 0", textAlign: "left" }}>
              {inspectModal.items.length === 0 ? (
                <p>{t("ui.nav.noItems")}</p>
              ) : (
                <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                  {inspectModal.items.map((item, idx) => (
                    <li key={idx} style={{ padding: "0.25rem 0", borderBottom: "1px solid var(--border, #eee)", display: "flex", justifyContent: "space-between" }}>
                      <span>{item.is_dir ? "📁 " : "📄 "}{item.name}</span>
                      <span style={{ fontSize: "0.85em", opacity: 0.7 }}>
                        {item.is_dir ? "" : `${item.size} B`}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div className="modal-actions">
              <button
                type="button"
                className="btn approve"
                onClick={() => {
                  const p = inspectModal.purpose;
                  setInspectModal(null);
                  if (liveAvailable) void pickLiveFolder(p);
                }}
              >
                Change Folder
              </button>
              <button type="button" className="btn ghost" onClick={() => setInspectModal(null)}>Close</button>
            </div>
          </div>
        </div>
      )}
    </nav>
  );
}
