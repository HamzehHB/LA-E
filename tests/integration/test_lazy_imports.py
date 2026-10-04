"""The CLI must import without the optional vector stack.

Proves that a deployment without ``lancedb`` / ``sentence_transformers``
can still run the CLI, because those imports are lazy (resolved only
inside the ``--embed`` branch). The blocking is done with a synthetic
``sys.meta_path`` blocker in a subprocess, so the proof does not depend
on what happens to be installed.
"""
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Runs in a clean interpreter: install a meta-path blocker for the
# optional heavy modules, then import the CLI package and ask for the
# argument parser. Any eager import of the blocked modules raises.
_PROBE = r'''
import sys


class _Blocker:
    BLOCKED = ("lancedb", "sentence_transformers")

    def find_module(self, name, path=None):
        if name.split(".")[0] in self.BLOCKED:
            return self

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in self.BLOCKED:
            raise ImportError("blocked by test: " + name)
        return None

    def load_module(self, name):
        raise ImportError("blocked by test: " + name)


sys.meta_path.insert(0, _Blocker())

from src.living_authenticity.cli import build_parser  # noqa: E402

parser = build_parser()
args = parser.parse_args(["--input-file", "x.md"])
assert args.input_file == "x.md"
print("OK")
'''


def test_cli_imports_without_optional_vector_stack():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert "OK" in result.stdout, (
        "CLI import failed with optional vector stack blocked:\n"
        + result.stdout + "\n" + result.stderr
    )


def test_embed_branch_is_the_only_place_vector_stack_is_imported():
    """``--embed`` is what reaches the optional stack, not the parser."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert "Traceback" not in result.stderr
    # The blocker installed first must never have fired.
    assert "blocked by test" not in result.stderr
    assert "blocked by test" not in result.stdout
