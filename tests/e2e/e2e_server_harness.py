"""E2E Server Harness for Playwright Browser Tests.

Launches the real loopback Python LauncherServer with genuine:
- Bootstrap injection into index.html
- Token authentication
- Live SSE stream (/api/events)
- Real pipeline with deterministic synthesis factory
- Workspace-confined isolated staging and audit directories
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import threading

# Ensure workspace root is in sys.path
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from src.living_authenticity.adapter.app import Launcher
from src.living_authenticity.adapter.server import LauncherServer
from src.living_authenticity.llm import parse_llm_result
from tests._synthetic_synthesis import synthetic_raw
from tests._test_authorizer import issue_test_authorizer
from tests.vault.test_vault_mode_e2e import create_synthetic_obsidian_vault


class DynamicE2ESynthesis:
    """Deterministic synthesis for E2E testing matching pipeline heuristics."""

    name = "dynamic_e2e"

    def __init__(self) -> None:
        self.calls = 0

    def synthesize(self, query=None, proposal=None, classification=None,
                   retrieval=None, comparisons=None, relation=None,
                   core=None, confidence=None, filter_outcome=None,
                   vault_context=None):
        self.calls += 1
        source_text = (
            getattr(query, "original_text", "")
            or getattr(query, "cleaned_text", "")
            or getattr(query, "normalized_text", "")
            or getattr(query, "meaning", "")
            or getattr(query, "source_text", "")
            or ""
        )
        if "bicyclists" in source_text.lower():
            title = "Urban Bicyclist Commutes"
            body = "Bicyclists navigate green corridors swiftly during evening commutes."
        elif "calm morning rituals" in source_text.lower():
            # Matches CREATE proposal path
            title = "Observed Calm Morning Rituals"
            body = "People often share calm morning rituals and the pattern repeats daily."
        else:
            # Matches non-CREATE (NEEDS_REVIEW) proposal path
            title = "Autonomous Deliberation"
            body = "Synthesizing empirical notes."

        raw = synthetic_raw(title=title, body=body)
        result = parse_llm_result(raw)
        return (result, "synthetic prompt", "0" * 64)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run LauncherServer for E2E tests")
    parser.add_argument("--web-root", type=str, default="frontend/dist",
                        help="Path to built frontend dist directory")
    parser.add_argument("--data-dir", type=str, default="",
                        help="Path to temporary data directory for staging and audit")
    args = parser.parse_args()

    web_root = Path(args.web_root).resolve()
    if not web_root.is_dir():
        print(f"ERROR: web-root not found: {web_root}", file=sys.stderr)
        sys.exit(1)

    authorized_output_dir = Path(os.path.realpath(WORKSPACE_ROOT / "tests" / "output"))
    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        data_dir = authorized_output_dir / "playwright_e2e" / ".e2e_data"

    # Enforce lexical path containment within authorized workspace test output directory
    resolved_data_dir = data_dir.resolve()
    try:
        resolved_data_dir.relative_to(authorized_output_dir)
    except ValueError:
        print(
            f"ERROR: data-dir '{data_dir}' is outside authorized test directory '{authorized_output_dir}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Enforce canonical realpath containment against symlinks and Windows NTFS junctions
    real_data_dir = Path(os.path.realpath(resolved_data_dir))
    try:
        real_data_dir.relative_to(authorized_output_dir)
    except ValueError:
        print(
            f"ERROR: data-dir '{data_dir}' resolves via symlink/junction to '{real_data_dir}' outside authorized test directory '{authorized_output_dir}'",
            file=sys.stderr,
        )
        sys.exit(1)

    data_dir = real_data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = data_dir / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    audit_dir = data_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    passed_dir = audit_dir / "passed"
    passed_dir.mkdir(parents=True, exist_ok=True)
    failed_dir = audit_dir / "failed"
    failed_dir.mkdir(parents=True, exist_ok=True)

    vault_dir = data_dir / "synthetic_vault"
    create_synthetic_obsidian_vault(vault_dir)

    def test_picker(purpose: str):
        if purpose == "vault":
            return ("configured", str(vault_dir), "")
        if purpose == "staging":
            return ("configured", str(staging_dir), "")
        if purpose == "audit":
            return ("configured", str(audit_dir), "")
        return ("cancelled", "", "cancelled")

    launcher = Launcher(
        web_root=str(web_root),
        paths={
            "staging": {"root": str(staging_dir)},
            "audit": {
                "root": str(audit_dir),
                "passed": str(passed_dir),
                "failed": str(failed_dir),
            },
        },
        pipeline_factory=None,
        synthesis_factory=lambda confirm=None, config=None: DynamicE2ESynthesis(),
        picker=test_picker,
        open_browser=False,
        is_test_mode=True,
        test_authorizer_factory=issue_test_authorizer,
    )

    server = LauncherServer(launcher, port=0, web_root=str(web_root))
    server.serve_in_thread()

    info = {
        "url": server.url,
        "port": server.port,
        "token": launcher.token,
        "instance_id": launcher.instance_id,
        "staging_dir": str(staging_dir),
        "audit_dir": str(audit_dir),
        "vault_dir": str(vault_dir),
    }
    # Print unambiguous ready signal to stdout for Playwright runner
    print("READY:" + json.dumps(info), flush=True)

    try:
        # Wait until stdin is closed by test harness
        sys.stdin.read()
    except (KeyboardInterrupt, Exception):
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
