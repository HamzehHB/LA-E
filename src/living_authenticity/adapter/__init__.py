"""Loopback launcher transport (integration layer).

This package is the ONLY application source subpath permitted to import
stdlib network modules (``http.server``), mirroring the narrow ``llm/``
network allowance in ``tests/security/test_execution_write_surface.py``.

Boundaries:
- binds exclusively to 127.0.0.1; per-launch ``secrets`` token on every
  non-health route; typed bounded errors; no stack traces;
- performs no direct filesystem writes (staging/audit writes stay inside
  the existing ``ControlledExecutor`` / ``AuditWriter`` boundaries);
- owns no pipeline logic: it invokes ``run_bounded_integration`` and the
  existing guards/discovery contracts only.
"""

__all__ = ["Launcher", "LauncherServer", "run_launcher"]

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import Launcher
    from .server import LauncherServer, run_launcher


def __getattr__(name):
    if name == "Launcher":
        from .app import Launcher as _Launcher

        return _Launcher
    if name in ("LauncherServer", "run_launcher"):
        from .server import LauncherServer as _LauncherServer
        from .server import run_launcher as _run_launcher

        return {"LauncherServer": _LauncherServer,
                "run_launcher": _run_launcher}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
