"""Bounded local-integration run package (composition only, no execution logic)."""
from typing import TYPE_CHECKING

from .api import (
    AUDIT_FAILED_PATH_KEY,
    AUDIT_PATH_KEY,
    AUDIT_PASSED_PATH_KEY,
    GUARDED_PATH_KEYS,
    STAGING_PATH_KEY,
    IntegrationRunReport,
    IntegrationUnitDetail,
    IntegrationUnitEntry,
    check_audit_eligible,
    check_roots_disjoint,
    check_staging_eligible,
    describe_index_state,
    index_corpus,
    index_units,
    ingest_units,
    render_review_block,
    resolve_authorized_audit_failed,
    resolve_authorized_audit_passed,
    resolve_authorized_audit_root,
    resolve_authorized_staging_root,
    resolve_guarded_roots,
    run_bounded_integration,
    synchronize_index,
)

# Vector-store helpers depend on the optional ``lancedb`` dependency
# through ``database.lancedb_manager``. They are resolved lazily so
# importing the integration package never requires the optional
# dependency. ``TYPE_CHECKING`` gives static analysers a real import to
# resolve ``__all__`` against, while runtime keeps the lazy path.
_VECTOR_EXPORTS = frozenset({
    "PRODUCTION_VECTOR_DB_KEY",
    "build_vector_components",
    "check_vector_store_eligible",
    "resolve_production_vector_db",
})

if TYPE_CHECKING:
    from .vector import (
        PRODUCTION_VECTOR_DB_KEY,
        build_vector_components,
        check_vector_store_eligible,
        resolve_production_vector_db,
    )

__all__ = [
    "AUDIT_FAILED_PATH_KEY",
    "AUDIT_PATH_KEY",
    "AUDIT_PASSED_PATH_KEY",
    "GUARDED_PATH_KEYS",
    "PRODUCTION_VECTOR_DB_KEY",
    "STAGING_PATH_KEY",
    "IntegrationRunReport",
    "IntegrationUnitDetail",
    "IntegrationUnitEntry",
    "build_vector_components",
    "check_audit_eligible",
    "check_roots_disjoint",
    "check_staging_eligible",
    "check_vector_store_eligible",
    "describe_index_state",
    "index_corpus",
    "index_units",
    "ingest_units",
    "render_review_block",
    "resolve_authorized_audit_failed",
    "resolve_authorized_audit_passed",
    "resolve_authorized_audit_root",
    "resolve_authorized_staging_root",
    "resolve_guarded_roots",
    "resolve_production_vector_db",
    "run_bounded_integration",
    "synchronize_index",
]


def __getattr__(name):
    """Resolve optional vector exports without an eager heavy import."""
    if name in _VECTOR_EXPORTS:
        from . import vector as _vector

        return getattr(_vector, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


