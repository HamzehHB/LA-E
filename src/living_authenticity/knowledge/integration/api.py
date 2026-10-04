"""Public entry point for the bounded local-integration run driver."""
from typing import TYPE_CHECKING

from .guard import (
    GUARDED_PATH_KEYS,
    STAGING_PATH_KEY,
    check_staging_eligible,
    resolve_authorized_staging_root,
    resolve_guarded_roots,
)
from .indexing import index_corpus, index_units, ingest_units
from .outcome import (
    IntegrationRunReport,
    IntegrationUnitDetail,
    IntegrationUnitEntry,
)
from .runner import run_bounded_integration
from .review_display import render_review_block

# Vector-store helpers depend on the optional ``lancedb`` dependency
# through ``database.lancedb_manager``. They stay importable under the
# same public names but are resolved lazily, so importing the
# integration package never requires the optional dependency.
# ``TYPE_CHECKING`` gives static analysers a real import to resolve
# ``__all__`` against, while runtime keeps the lazy ``__getattr__`` path.
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
    "GUARDED_PATH_KEYS",
    "PRODUCTION_VECTOR_DB_KEY",
    "STAGING_PATH_KEY",
    "IntegrationRunReport",
    "IntegrationUnitDetail",
    "IntegrationUnitEntry",
    "build_vector_components",
    "check_staging_eligible",
    "check_vector_store_eligible",
    "index_corpus",
    "index_units",
    "ingest_units",
    "render_review_block",
    "resolve_authorized_staging_root",
    "resolve_guarded_roots",
    "resolve_production_vector_db",
    "run_bounded_integration",
]


def __getattr__(name):
    """Resolve optional vector exports without an eager heavy import."""
    if name in _VECTOR_EXPORTS:
        from . import vector as _vector

        return getattr(_vector, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")





