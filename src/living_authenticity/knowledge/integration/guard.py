"""Staging-eligibility guard for bounded local-integration runs.

Composition only, no new authority. All containment comparisons reuse
the existing ``PathBoundary`` resolution and normalization.

Trust model: this guard prevents *misconfiguration*, not an in-process
adversary. The guarded roots and the authorized staging destination are
resolved from the deployment's own configuration
(``Config/paths.local.yaml`` via ``Config/settings.py``); a caller that
deliberately overrides both could authorize anything, exactly as it could
by shrinking the guarded-root set. What the guard guarantees is that a
deployment's declared staging exception is narrow (one exact path),
explicit, config-declared, and cannot be widened by a path spelling
trick — no arbitrary child of a guarded root can become a staging
destination.
"""
from pathlib import Path

from Config.settings import ROOT as REPOSITORY_ROOT
from Config.settings import load_paths
from src.living_authenticity.security.path_boundary import PathBoundary

from .outcome import IntegrationRunReport

GUARDED_PATH_KEYS = (
    "data.root",
    "models.bge_m3",
    "vector_db.lancedb",
    "memory.root",
    "obsidian.vault",
    "zotero.library",
    "exports.root",
    "cache.root",
)

# The single configuration key that may authorize a staging destination
# inside guarded persistent data.
STAGING_PATH_KEY = "staging.root"

_ALLOWED_SOURCE_SUFFIXES = frozenset({".md", ".txt"})



def _lookup(data: dict, dotted_key: str):
    """Walk nested mappings for one dotted key; return None when absent."""
    value = data
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def resolve_guarded_roots(config=None) -> tuple:
    """Return guarded persistent-data roots from the existing config."""
    data = config if isinstance(config, dict) else load_paths()
    roots = []
    for key in GUARDED_PATH_KEYS:
        value = _lookup(data, key)
        if isinstance(value, str) and value.strip():
            roots.append(value)
    return tuple(roots)


def resolve_authorized_staging_root(config=None) -> str:
    """Return the single config-declared staging destination, or ``""``.

    Empty means the deployment authorizes no staging location inside
    guarded persistent data; the ordinary guards then apply unchanged.
    """
    data = config if isinstance(config, dict) else load_paths()
    value = _lookup(data, STAGING_PATH_KEY)
    return value.strip() if isinstance(value, str) else ""


def _resolves_identically(first, second) -> bool:
    """True when both paths resolve to the same location.

    Uses ``PathBoundary`` resolution/normalization in both directions
    instead of string comparison, so case, separator, trailing-slash,
    relative-form, and symlink spellings of the same location compare
    equal while different locations never do.
    """
    return (PathBoundary(first).is_allowed(second)
            and PathBoundary(second).is_allowed(first))


def check_staging_eligible(staging_root, guarded_roots=(),
                           authorized_staging_root="") -> tuple:
    """Return ``(eligible, reason, check)`` for one staging candidate.

    ``staging_root`` must be explicitly supplied, exist, be a real
    directory, not be a symlink, and resolve outside the repository.
    Inside guarded persistent data it is eligible only when it resolves
    identically to the single config-declared ``staging.root``.
    """
    if not isinstance(staging_root, str) or not staging_root.strip():
        return (False, "staging root missing", "destination")
    candidate = Path(staging_root)
    if candidate.is_symlink():
        return (False, "staging root is a symlink", "destination")
    if not candidate.is_dir():
        return (False, "staging root missing or not a directory", "destination")
    if PathBoundary(str(REPOSITORY_ROOT)).is_allowed(staging_root):
        return (False, "staging inside repository workspace", "destination")
    guarded = tuple(guarded_roots) if guarded_roots is not None else ()
    if not guarded or not PathBoundary(*guarded).is_allowed(staging_root):
        return (True, "", "")
    authorized = (
        authorized_staging_root.strip()
        if isinstance(authorized_staging_root, str) else ""
    )
    if not authorized:
        return (False, "staging inside guarded persistent data", "destination")
    if not PathBoundary(staging_root).is_allowed(authorized):
        return (False, "staging is outside the authorized staging location",
                "destination")
    if not _resolves_identically(staging_root, authorized):
        return (False, "staging resolves differently from the authorized "
                       "staging location", "destination")
    return (True, "", "")


