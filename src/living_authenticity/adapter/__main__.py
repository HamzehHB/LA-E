"""Run the loopback launcher: ``python -m src.living_authenticity.adapter``.

Interim delivery step: the application opens in the user's default system
browser. A dedicated application window is a separate packaging decision.
"""


def main() -> int:
    from pathlib import Path

    from Config.settings import ROOT

    from .server import run_launcher

    web_root = ROOT / "frontend" / "dist"
    run_launcher(web_root=str(web_root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
