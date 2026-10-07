"""Local/cloud model discovery and selection contract (read-only).

Discovery lists what the user's configured model environment offers so
a later UI can present it as clickable choices. It never downloads,
installs, modifies, deletes, renames, or moves model files, never
exposes credentials, and never changes the selected provider: selection
stays a configuration decision validated by :mod:`registry`.
"""
import os

from .locality import is_local_endpoint  # noqa: F401  (re-export guard)

LOCAL_CATEGORY = "local"
CLOUD_CATEGORY = "cloud"

CATEGORIES = (LOCAL_CATEGORY, CLOUD_CATEGORY)

# Filename suffixes treated as local model artifacts during discovery.
# The scan is name-based and read-only; content is never executed.
LOCAL_MODEL_SUFFIXES = frozenset({".gguf", ".bin", ".safetensors", ".pt"})

# Cloud catalog: provider-agnostic placeholder entries. Real deployments
# override model names through ``llm.local.yaml``; nothing here names a
# vendor default as authoritative.
CLOUD_CATALOG = (
    {"provider": "cloud", "model": "<your-cloud-model>",
     "category": CLOUD_CATEGORY},
)


def discover_local_models(model_root, max_entries: int = 50) -> dict:
    """Return ``{"models": [...], "skipped": n}`` for ``model_root``.

    Read-only: lists model-like files (bounded, sorted, no recursion
    into symlinked directories, confined by resolved-path containment
    under ``model_root``). A discovered model is not trusted — the LLM
    contract and validation stay authoritative.
    """
    if not isinstance(model_root, str) or not model_root.strip():
        return {"models": [], "skipped": 1, "reason": "model root missing"}
    if os.path.islink(model_root) or not os.path.isdir(model_root):
        return {"models": [], "skipped": 1, "reason": "model root unavailable"}
    try:
        boundary_root = os.path.realpath(model_root)
    except Exception:
        return {"models": [], "skipped": 1, "reason": "model root invalid"}
    if not isinstance(max_entries, int) or isinstance(max_entries, bool):
        return {"models": [], "skipped": 1, "reason": "max_entries invalid"}
    if max_entries < 1:
        return {"models": [], "skipped": 1, "reason": "max_entries invalid"}
    candidates = []
    try:
        for current, directories, filenames in os.walk(model_root):
            directories[:] = sorted(
                name for name in directories
                if not os.path.islink(os.path.join(current, name)))
            for filename in sorted(filenames):
                candidates.append(os.path.join(current, filename))
    except OSError:
        return {"models": [], "skipped": 1, "reason": "model root unreadable"}
    models = []
    skipped = 0
    for child in candidates:
        if len(models) >= max_entries:
            skipped += 1
            continue
        name = os.path.basename(child)
        extension = os.path.splitext(name)[1].lower()
        if extension not in LOCAL_MODEL_SUFFIXES:
            continue
        if os.path.islink(child) or not os.path.isfile(child):
            continue
        try:
            resolved = os.path.realpath(child)
        except Exception:
            skipped += 1
            continue
        if os.path.commonpath([boundary_root, resolved]) != boundary_root:
            skipped += 1
            continue
        models.append({"name": os.path.splitext(name)[0], "filename": name,
                       "category": LOCAL_CATEGORY})
    return {"models": models, "skipped": skipped, "reason": ""}


def describe_selection(config) -> dict:
    """Return the validated provider/model selection for ``config``.

    Never includes credential values — only the environment variable
    name and whether it is set (checked by the caller, not stored).
    """
    section = config.get("llm", {}) if isinstance(config, dict) else {}
    provider = section.get("provider", "") if isinstance(section, dict) else ""
    provider = provider.strip().lower() if isinstance(provider, str) else ""
    model = section.get("model", "") if isinstance(section, dict) else ""
    model = model.strip() if isinstance(model, str) else ""
    endpoint = section.get("endpoint", "") if isinstance(section, dict) else ""
    endpoint = endpoint.strip() if isinstance(endpoint, str) else ""
    credential_env = section.get("credential_env", "")
    credential_env = (credential_env.strip()
                      if isinstance(credential_env, str) else "")
    category = LOCAL_CATEGORY if provider in ("ollama", "disabled") else CLOUD_CATEGORY
    if provider not in ("disabled", "ollama", "cloud"):
        return {"valid": False, "reason": "unknown llm.provider",
                "provider": provider, "category": category}
    if provider in ("ollama", "cloud") and not model:
        return {"valid": False, "reason": "llm.model must be set",
                "provider": provider, "category": category}
    if provider == "cloud" and not endpoint:
        return {"valid": False, "reason": "llm.endpoint must be set",
                "provider": provider, "category": category}
    return {"valid": True, "reason": "", "provider": provider or "disabled",
            "model": model, "endpoint": endpoint,
            "credential_env": credential_env, "category": category}


__all__ = ("LOCAL_CATEGORY", "CLOUD_CATEGORY", "CATEGORIES",
           "LOCAL_MODEL_SUFFIXES", "CLOUD_CATALOG",
           "discover_local_models", "describe_selection")
