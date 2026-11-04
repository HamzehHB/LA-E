"""Authorized Test Harness Issuance Point.

This module resides strictly within the test suite (`tests/`) and provides
an explicit entry point for test code to obtain `TestModeAuthorizer` capabilities.

Trust Model:
Within a single Python process, code executes in shared memory. This module establishes
an explicit, non-ambient architectural boundary separating test-mode output exceptions
from production execution. It does NOT claim to provide an OS-level security ring or
protection against malicious code executing in the same interpreter.

Initialization:
Upon initial import, this module generates a cryptographically random session secret
and registers it once with the guard system via `register_test_harness_secret()`.
If registration fails (for example, if another component has already registered a secret),
initialization fails fast with a RuntimeError. Registration errors are never silently swallowed,
and authorizers are never issued with an unregistered or mismatched secret.
"""

import secrets

from src.living_authenticity.knowledge.integration.guard import (
    register_test_harness_secret,
    issue_test_authorizer as _guard_issue_test_authorizer,
    TestModeAuthorizer,
    authorized_test_mode,
    is_test_mode_authorized,
)


def _initialize_test_harness_secret() -> str:
    """Generate and register the test harness session secret once.

    Raises:
        RuntimeError: If registration fails because another component has already
            registered a secret or the registration contract is violated.
    """
    secret = secrets.token_hex(32)
    try:
        register_test_harness_secret(secret)
    except PermissionError as exc:
        raise RuntimeError(
            "Failed to initialize test harness authorizer: a test harness secret has already been registered "
            "in this process. Cannot initialize with an unregistered or mismatched secret."
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"Failed to initialize test harness authorizer: {exc}"
        ) from exc
    return secret


_SESSION_SECRET = _initialize_test_harness_secret()


def issue_test_authorizer(purpose: str = "test") -> TestModeAuthorizer:
    """Issue a single-use TestModeAuthorizer capability for tests.

    Supplies the active test harness session secret to the guard system.
    """
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("issue_test_authorizer requires a non-empty purpose description")
    return _guard_issue_test_authorizer(purpose=purpose, harness_secret=_SESSION_SECRET)


__all__ = (
    "issue_test_authorizer",
    "TestModeAuthorizer",
    "authorized_test_mode",
    "is_test_mode_authorized",
)
