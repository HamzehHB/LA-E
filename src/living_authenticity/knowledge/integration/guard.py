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
    "audit.root",
)

# The single configuration key that may authorize a staging destination
# inside guarded persistent data.
STAGING_PATH_KEY = "staging.root"

# The single configuration key that declares the persistent-audit
# destination. Audit records are append-only traceability; the writer may
# create this root when configured, and nothing else may write through it.
AUDIT_PATH_KEY = "audit.root"

# Optional subroots for the two audit storage categories. They must
# resolve inside ``audit.root``; defaults are derived when absent.
AUDIT_PASSED_PATH_KEY = "audit.passed"
AUDIT_FAILED_PATH_KEY = "audit.failed"

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


def resolve_authorized_audit_root(config=None) -> str:
    """Return the single config-declared persistent-audit destination."""
    data = config if isinstance(config, dict) else load_paths()
    value = _lookup(data, AUDIT_PATH_KEY)
    return value.strip() if isinstance(value, str) else ""


def resolve_authorized_audit_passed(config=None) -> str:
    """Return the config-declared passed-audit subroot, or ``""``."""
    data = config if isinstance(config, dict) else load_paths()
    value = _lookup(data, AUDIT_PASSED_PATH_KEY)
    return value.strip() if isinstance(value, str) else ""


def resolve_authorized_audit_failed(config=None) -> str:
    """Return the config-declared failed-audit subroot, or ``""``."""
    data = config if isinstance(config, dict) else load_paths()
    value = _lookup(data, AUDIT_FAILED_PATH_KEY)
    return value.strip() if isinstance(value, str) else ""


def _subroot_inside_audit(sub_root: str, authorized: str) -> tuple:
    """Return ``(ok, reason)`` for one optional audit category subroot."""
    if not sub_root.strip():
        return (True, "")
    if _resolves_identically(sub_root, authorized):
        return (False, "must be a subroot, not audit.root itself")
    try:
        boundary = PathBoundary(authorized)
    except Exception:
        return (False, "invalid")
    try:
        if not boundary.is_allowed(sub_root):
            return (False, "resolves outside audit.root")
    except Exception:
        return (False, "invalid")
    candidate = Path(sub_root)
    if candidate.is_symlink():
        return (False, "is a symlink")
    return (True, "")


def check_audit_eligible(audit_root, guarded_roots=(),
                         authorized_audit_root="", authorized_passed="",
                         authorized_failed="") -> tuple:
    """Return ``(eligible, reason, check)`` for one audit candidate.

    The candidate must be explicitly supplied, outside the repository,
    and resolve identically to the single config-declared ``audit.root``.
    Overlap with any other guarded root (vault, staging, vector db) is
    rejected so audit records can never share storage with source,
    staging, or index data.

    If ``authorized_passed`` or ``authorized_failed`` are provided, they
    are also validated to resolve inside the audit root (not equal to it).
    """
    if not isinstance(audit_root, str) or not audit_root.strip():
        return (False, "audit root missing", "audit")
    candidate = Path(audit_root)
    if candidate.is_symlink():
        return (False, "audit root is a symlink", "audit")
    if candidate.exists() and not candidate.is_dir():
        return (False, "audit root is not a directory", "audit")
    if PathBoundary(str(REPOSITORY_ROOT)).is_allowed(audit_root):
        return (False, "audit inside repository workspace", "audit")
    authorized = (
        authorized_audit_root.strip()
        if isinstance(authorized_audit_root, str) else ""
    )
    if not authorized:
        return (False, "no authorized audit location configured", "audit")
    if not _resolves_identically(audit_root, authorized):
        return (False, "audit resolves differently from the authorized "
                       "audit location", "audit")
    for sub_name, sub_root in (("passed", authorized_passed),
                               ("failed", authorized_failed)):
        if isinstance(sub_root, str) and sub_root.strip():
            ok, detail = _subroot_inside_audit(sub_root.strip(), authorized)
            if not ok:
                return (False, f"audit.{sub_name} {detail}", "audit")
    if (isinstance(authorized_passed, str) and authorized_passed.strip()
            and isinstance(authorized_failed, str)
            and authorized_failed.strip()):
        if _resolves_identically(authorized_passed.strip(),
                                 authorized_failed.strip()):
            return (False, "audit.passed and audit.failed must differ",
                    "audit")
    guarded = tuple(guarded_roots) if guarded_roots is not None else ()
    others = [root for root in guarded
              if not _resolves_identically(root, authorized)]
    if others and PathBoundary(*others).is_allowed(audit_root):
        return (False, "audit overlaps another guarded root", "audit")
    return (True, "", "")


def check_roots_disjoint(staging_root="", audit_root="",
                         vector_db_path="", vault_root="") -> tuple:
    """Return ``(disjoint, reason, check)`` for configured persistent roots.

    Uses resolved-path comparison in both directions; any identical or
    containing relationship between distinct roots is rejected.
    """
    pairs = (
        ("staging", staging_root, "audit", audit_root),
        ("staging", staging_root, "vector_db", vector_db_path),
        ("staging", staging_root, "vault", vault_root),
        ("audit", audit_root, "vector_db", vector_db_path),
        ("audit", audit_root, "vault", vault_root),
        ("vector_db", vector_db_path, "vault", vault_root),
    )
    for first_name, first, second_name, second in pairs:
        if not (isinstance(first, str) and first.strip()):
            continue
        if not (isinstance(second, str) and second.strip()):
            continue
        try:
            first_allows = PathBoundary(first).is_allowed(second)
            second_allows = PathBoundary(second).is_allowed(first)
        except Exception:
            continue
        if first_allows or second_allows:
            return (False, first_name + " overlaps " + second_name,
                    "paths")
    return (True, "", "")


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
