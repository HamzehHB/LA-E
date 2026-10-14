"""Native folder selection for the launcher (closed purpose enum).

The dialog runs inside the launcher process on the user's machine; the
frontend can only request a purpose and receives a typed, backend-validated
result. No frontend-supplied absolute path is ever accepted, and no
selection is persisted here (configuration persistence is deferred).
"""
from pathlib import Path

from Config.settings import ROOT as REPOSITORY_ROOT

# Closed purpose enum. The frontend never selects arbitrary paths.
FOLDER_PURPOSES = (
    "vault",
    "staging",
    "audit",
    "vector_db",
    "local_llm",
    "embedding_model",
)

# Status vocabulary (mirrors frontend transport contract).
STATUS_CONFIGURED = "configured"
STATUS_INVALID = "invalid"
STATUS_UNAVAILABLE = "unavailable"
STATUS_CANCELLED = "cancelled"


def normalize_purpose(value) -> str:
    if isinstance(value, str) and value in FOLDER_PURPOSES:
        return value
    raise ValueError("purpose must be one of: " + ", ".join(FOLDER_PURPOSES))


def display_for(path) -> str:
    """Safe, non-authoritative display label (basename only)."""
    if not isinstance(path, str) or not path.strip():
        return ""
    try:
        return Path(path).name or str(path)
    except OSError:
        return ""


def tkinter_pick(purpose: str):
    """Open the native OS folder dialog on a dedicated thread.

    Returns ``(status, path, reason)``: ``"cancelled"`` when the user
    closes the dialog, ``"unavailable"`` when no native dialog can be
    opened (headless), else ``"selected"`` with the absolute path.
    No path is treated as validated here.
    """
    import threading

    result: dict = {}

    def _dialog():
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            try:
                root.attributes("-topmost", True)
            except Exception:
                pass
            path = filedialog.askdirectory(
                title="Select folder: " + purpose)
            try:
                root.destroy()
            except Exception:
                pass
            result["path"] = path or ""
        except Exception:
            result["path"] = ""
            result["error"] = True

    worker = threading.Thread(target=_dialog, daemon=True)
    worker.start()
    worker.join()
    if result.get("error"):
        return (STATUS_UNAVAILABLE, "",
                "native folder dialog unavailable on this system")
    if not result.get("path"):
        return (STATUS_CANCELLED, "", "selection cancelled")
    return (STATUS_CONFIGURED, str(result["path"]), "")


def validate_purpose(purpose: str, path: str,
                     paths=None) -> tuple:
    """Backend-authoritative validation of one selected path.

    Returns ``(status, display, reason)``. Reuses the existing guards:
    ``check_staging_eligible``, ``check_audit_eligible``,
    ``check_vector_store_eligible``; other purposes get the established
    basic directory/containment validation used by the CLI.
    """
    from src.living_authenticity.knowledge.integration.guard import (
        check_audit_eligible,
        check_staging_eligible,
        resolve_authorized_audit_failed,
        resolve_authorized_audit_passed,
        resolve_authorized_audit_root,
        resolve_authorized_staging_root,
        resolve_guarded_roots,
    )
    from src.living_authenticity.knowledge.integration.vector import (
        check_vector_store_eligible,
        resolve_production_vector_db,
    )
    from src.living_authenticity.security.path_boundary import PathBoundary

    if not isinstance(path, str) or not path.strip():
        return (STATUS_INVALID, "", "empty path")
    candidate = Path(path)
    if candidate.is_symlink():
        return (STATUS_INVALID, "", "selected path is a symlink")
    if not candidate.is_dir():
        return (STATUS_INVALID, "",
                "selected path is not an existing directory")
    if PathBoundary(str(REPOSITORY_ROOT)).is_allowed(path):
        return (STATUS_INVALID, "",
                "selected path is inside the repository workspace")

    guarded = resolve_guarded_roots(paths)
    if purpose == "staging":
        eligible, reason, _check = check_staging_eligible(
            path, guarded, resolve_authorized_staging_root(paths))
        return ((STATUS_CONFIGURED if eligible else STATUS_INVALID),
                display_for(path), reason)
    if purpose == "audit":
        eligible, reason, _check = check_audit_eligible(
            path, guarded,
            resolve_authorized_audit_root(paths),
            resolve_authorized_audit_passed(paths),
            resolve_authorized_audit_failed(paths))
        return ((STATUS_CONFIGURED if eligible else STATUS_INVALID),
                display_for(path), reason)
    if purpose == "vector_db":
        data = paths if isinstance(paths, dict) else {}
        vault_value = data.get("obsidian", {}).get("vault", "")
        vault = vault_value if isinstance(vault_value, str) else ""
        eligible, reason, _check = check_vector_store_eligible(
            path,
            resolve_production_vector_db(data),
            vault,
            resolve_authorized_audit_root(data),
            resolve_authorized_staging_root(data))
        return ((STATUS_CONFIGURED if eligible else STATUS_INVALID),
                display_for(path), reason)
    # vault / local_llm / embedding_model: validated as a usable external
    # directory (existence, non-symlink, containment) — backend-owned.
    return (STATUS_CONFIGURED, display_for(path), "")


def pick_and_validate(purpose: str, picker=None,
                      paths=None) -> tuple:
    """Run the native picker, then validate the result authoritatively."""
    purpose = normalize_purpose(purpose)
    choose = picker if picker is not None else tkinter_pick
    status, path, reason = choose(purpose)
    if status != STATUS_CONFIGURED:
        return (status, "", reason)
    return validate_purpose(purpose, path, paths)


__all__ = [
    "FOLDER_PURPOSES",
    "STATUS_CONFIGURED",
    "STATUS_INVALID",
    "STATUS_UNAVAILABLE",
    "STATUS_CANCELLED",
    "normalize_purpose",
    "display_for",
    "tkinter_pick",
    "validate_purpose",
    "pick_and_validate",
]
