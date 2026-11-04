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
import asyncio
import contextvars
from contextlib import contextmanager
import hmac
from pathlib import Path
import threading
from typing import Any
import uuid

from Config.settings import ROOT as REPOSITORY_ROOT
from Config.settings import load_paths
from src.living_authenticity.security.path_boundary import PathBoundary

from .outcome import IntegrationRunReport

# Private sentinel to restrict direct construction of TestModeAuthorizer
_ISSUANCE_SECRET = object()

# One-time registration for the authorized test harness secret
_HARNESS_SECRET: str | None = None
_HARNESS_REGISTERED: bool = False
_HARNESS_LOCK = threading.Lock()


def register_test_harness_secret(secret: str) -> None:
    """One-time registration of the authorized test harness secret.

    Can only be invoked once upon test runner / test harness initialization.
    Subsequent attempts fail closed, preventing unauthorized overwrite.
    """
    global _HARNESS_SECRET, _HARNESS_REGISTERED
    with _HARNESS_LOCK:
        if _HARNESS_REGISTERED:
            raise PermissionError("Test harness authority has already been registered")
        if not isinstance(secret, str) or len(secret) < 32:
            raise ValueError("Harness secret must be a string of at least 32 characters")
        _HARNESS_SECRET = secret
        _HARNESS_REGISTERED = True


# ContextVar-backed authorization: safe for async tasks on the same thread
# and cannot leak across concurrent coroutines.
_TEST_MODE_CONTEXT_VAR: contextvars.ContextVar[object] = contextvars.ContextVar(
    "_test_mode_auth", default=None
)


class TestOutputAuthorizationToken:
    """Explicit capability token required to authorize test output exceptions.

    Stateful validity prevents authorization from outliving its lexical scope,
    even when an execution context or ContextVar is cloned by asyncio tasks.

    In-process trust model:
    Within a single Python runtime, all modules execute in shared process memory.
    This capability pattern enforces an explicit, fail-closed authorization boundary
    that prevents accidental test-mode activation, ambient authority leakage, or
    unintentional propagation to child tasks. It does not claim to act as an OS-level
    security sandbox against deliberate in-process introspection.
    """

    __slots__ = ("_is_active", "_authorizer_id", "_thread_id", "_task")

    def __init__(self, authorizer: Any):
        if not isinstance(authorizer, TestModeAuthorizer) or not authorizer.is_active:
            raise PermissionError(
                "TestOutputAuthorizationToken can only be minted via authorized_test_mode() with an active TestModeAuthorizer"
            )
        self._is_active = True
        self._authorizer_id = authorizer.authorizer_id
        self._thread_id = threading.get_ident()
        try:
            self._task = asyncio.current_task()
        except RuntimeError:
            self._task = None

    @property
    def authorizer_id(self) -> str:
        return self._authorizer_id

    @property
    def is_active(self) -> bool:
        return self._is_active

    def _invalidate(self) -> None:
        self._is_active = False


class TestModeAuthorizer:
    """Explicit capability object representing an issued authorization to activate test mode.

    Cannot be directly instantiated via a public zero-argument constructor.
    Instances must be explicitly issued by the authorized test harness (`tests._test_authorizer`).
    Direct context-manager activation (__enter__/__exit__) is intentionally not supported
    to ensure all activations pass through the validated `authorized_test_mode(authorizer)` boundary.

    Single-use atomic lifecycle:
    - An authorizer can be consumed at most once.
    - Consumption is atomic upon entering `authorized_test_mode(authorizer)`.
    - Any concurrent, sequential, or nested attempt to reuse the same authorizer instance fails closed.
    - Upon scope exit (or exception), the authorizer is permanently invalidated.

    In-process trust model:
    Within a single Python runtime, all code shares the same interpreter memory.
    This capability pattern enforces an explicit, non-ambient architectural boundary
    preventing application code from accidentally or implicitly activating test-mode bypasses.
    It does not claim to act as an OS-level security ring against deliberate in-process introspection.
    """

    __slots__ = ("_authorizer_id", "_is_active", "_is_consumed", "_is_in_use", "_lock")

    def __init__(self, issuance_secret: object = None):
        if issuance_secret is not _ISSUANCE_SECRET:
            raise PermissionError(
                "TestModeAuthorizer cannot be directly instantiated. "
                "Authorization must be obtained via the authorized test harness."
            )
        self._authorizer_id = uuid.uuid4().hex
        self._is_active = True
        self._is_consumed = False
        self._is_in_use = False
        self._lock = threading.Lock()

    @property
    def authorizer_id(self) -> str:
        return self._authorizer_id

    @property
    def is_active(self) -> bool:
        return self._is_active

    @property
    def is_consumed(self) -> bool:
        return self._is_consumed

    @property
    def is_in_use(self) -> bool:
        return self._is_in_use

    def reserve(self) -> None:
        """Atomically reserve this authorizer for an authorization scope.

        Fails closed if the authorizer is inactive, already consumed, or currently in use.
        """
        with self._lock:
            if not self._is_active or self._is_consumed or self._is_in_use:
                raise PermissionError(
                    "authorized_test_mode rejected: TestModeAuthorizer has already been used or consumed"
                )
            self._is_consumed = True
            self._is_in_use = True

    def invalidate(self) -> None:
        """Permanently invalidate this authorizer upon scope exit or cancellation."""
        with self._lock:
            self._is_active = False
            self._is_in_use = False
            self._is_consumed = True


def issue_test_authorizer(purpose: str = "test", harness_secret: str = "") -> TestModeAuthorizer:
    """Restricted issuance factory for TestModeAuthorizer capabilities.

    Ordinary application code cannot obtain authorization by calling this function.
    Issuance strictly requires the active session secret possessed exclusively by
    the authorized test harness (`tests._test_authorizer`).
    """
    with _HARNESS_LOCK:
        if not _HARNESS_REGISTERED or not _HARNESS_SECRET:
            raise PermissionError(
                "Test mode authorization is unavailable: no authorized test harness has been registered"
            )
        if (
            not isinstance(harness_secret, str)
            or not harness_secret
            or not hmac.compare_digest(harness_secret, _HARNESS_SECRET)
        ):
            raise PermissionError(
                "Unauthorized test mode authorizer issuance: invalid or missing harness secret. "
                "Ordinary application code cannot issue test authorizers."
            )
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("issue_test_authorizer requires a non-empty purpose description")
    return TestModeAuthorizer(_ISSUANCE_SECRET)


@contextmanager
def authorized_test_mode(authorizer: Any = None):
    """Explicitly controlled test-mode entry point.

    Requires an explicit, active TestModeAuthorizer capability instance. Ordinary callers,
    unauthorized application code, or code executing without an active authorizer cannot
    activate test output exceptions, even if they pass allow_test_output=True.

    Atomically reserves the authorizer upon entry to prevent any concurrent, sequential,
    or nested reuse of the same capability instance.
    """
    if not isinstance(authorizer, TestModeAuthorizer):
        raise PermissionError(
            "authorized_test_mode requires an explicit TestModeAuthorizer capability"
        )
    # Atomically reserve on entry: raises PermissionError if inactive, consumed, or in-use
    authorizer.reserve()

    token = TestOutputAuthorizationToken(authorizer)
    reset_token = _TEST_MODE_CONTEXT_VAR.set(token)
    try:
        yield token
    finally:
        token._invalidate()
        authorizer.invalidate()
        _TEST_MODE_CONTEXT_VAR.reset(reset_token)


def is_test_mode_authorized(auth: Any = None) -> bool:
    """Return True if execution is within an explicitly authorized test context."""
    current = _TEST_MODE_CONTEXT_VAR.get(None)
    if not isinstance(current, TestOutputAuthorizationToken) or not current.is_active:
        return False
    if current._thread_id != threading.get_ident():
        return False
    try:
        cur_task = asyncio.current_task()
    except RuntimeError:
        cur_task = None
    if current._task is not None and cur_task is not current._task:
        return False

    if isinstance(auth, TestOutputAuthorizationToken):
        if auth is not current or not auth.is_active or auth.authorizer_id != current.authorizer_id:
            return False
    elif isinstance(auth, TestModeAuthorizer):
        if not auth.is_active or not auth.is_in_use or auth.authorizer_id != current.authorizer_id:
            return False
    elif auth is not None and auth is not True:
        # Invalid or mismatched authorization argument fails closed
        return False

    return True

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
                         authorized_failed="",
                         allow_test_output: bool = False) -> tuple:
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
    test_output_root = (REPOSITORY_ROOT / "tests" / "output").resolve()
    candidate_resolved = candidate.expanduser().resolve()
    is_inside_repo = PathBoundary(str(REPOSITORY_ROOT)).is_allowed(audit_root)
    is_strict_descendant = (
        test_output_root in candidate_resolved.parents
        and candidate_resolved != test_output_root
    )
    is_test_auth = bool(allow_test_output) and is_test_mode_authorized(allow_test_output)
    if is_inside_repo:
        if not (is_test_auth and is_strict_descendant):
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
                           authorized_staging_root="",
                           allow_test_output: bool = False) -> tuple:
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
    test_output_root = (REPOSITORY_ROOT / "tests" / "output").resolve()
    candidate_resolved = candidate.expanduser().resolve()
    is_inside_repo = PathBoundary(str(REPOSITORY_ROOT)).is_allowed(staging_root)
    is_strict_descendant = (
        test_output_root in candidate_resolved.parents
        and candidate_resolved != test_output_root
    )
    is_test_auth = bool(allow_test_output) and is_test_mode_authorized(allow_test_output)
    if is_inside_repo:
        if not (is_test_auth and is_strict_descendant):
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
