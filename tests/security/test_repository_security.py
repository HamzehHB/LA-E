import re
import subprocess
from pathlib import Path

from src.living_authenticity.security import (
    MACHINE_SPECIFIC_PATH_PREFIXES,
    SECRET_PATTERNS,
)

REPO = Path(__file__).resolve().parents[2]

# Files that necessarily contain detection-pattern strings for the
# secret-pattern scan: the pattern definition module and the detector's
# own test fixtures (which must construct fake secrets to prove detection).
SECRET_PATTERN_EXEMPT_FILES = (
    "src/living_authenticity/security/sensitive_data.py",
    "tests/security/test_sensitive_data.example.py",
)

# Files exempt from the machine-path scan: only the pattern definition
# module itself. Tracked tests must use synthetic fixture prefixes via
# monkeypatching (see tests/security/test_sensitive_data.example.py); real-path
# assertions live only under gitignored tests/local/.
PATH_PREFIX_EXEMPT_FILES = (
    "src/living_authenticity/security/sensitive_data.py",
)

IGNORED_ENTRIES = (
    "Config/paths.local.yaml",
    "Config/llm.local.yaml",
    ".project/",
    ".clinerules/",
    "Venv/",
    ".env",
    "Logs/*",
    "tests/local/",
)

CHECK_IGNORE_PATHS = (
    "Config/paths.local.yaml",
    "Config/llm.local.yaml",
    ".project/",
    ".clinerules/",
    "Venv/",
    ".env",
    "Logs/dev.log",
    "tests/local/test_real_ollama_local.py",
)

UNSAFE_CODE_SNIPPETS = (
    "subprocess",
    "os.system(",
    "eval(",
    "exec(",
    "__import__",
    "shell=True",
    "pickle",
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout


def _tracked_files() -> list[str]:
    return [line for line in _git("ls-files").splitlines() if line]


def test_no_known_secret_patterns_in_tracked_files():
    for path in _tracked_files():
        if path in SECRET_PATTERN_EXEMPT_FILES:
            continue
        content = (REPO / path).read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            assert pattern not in content, (
                f"{path} contains secret pattern {pattern!r}"
            )


def test_production_and_repo_roots_absent_from_tracked_files():
    for path in _tracked_files():
        if path in PATH_PREFIX_EXEMPT_FILES:
            continue
        content = (REPO / path).read_text(encoding="utf-8", errors="replace")
        normalized = re.sub(
            r'"\s*\+\s*"', "", content,
        )
        for prefix in MACHINE_SPECIFIC_PATH_PREFIXES:
            assert prefix not in content, (
                f"{path} contains machine-specific path {prefix!r}"
            )
            assert prefix not in normalized, (
                f"{path} contains obfuscated machine-specific path {prefix!r}"
            )


def test_ignore_rules_cover_private_entries():
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")

    for entry in IGNORED_ENTRIES:
        assert entry in gitignore, f".gitignore does not cover {entry}"

    ignored = _git("check-ignore", *CHECK_IGNORE_PATHS).splitlines()
    assert len(ignored) == len(CHECK_IGNORE_PATHS), (
        f"git check-ignore reported: {ignored}"
    )


def test_security_policy_exists_and_aligns_with_governance():
    raw = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    security = " ".join(raw.split())

    assert "AI-Governance.md" in security
    assert "Coding-Agent-Access.md" in security
    assert "denied to coding agents by default" in security
    assert "never be committed or pushed" in security
    assert "CREATE" in security and "NEEDS_REVIEW" in security


def test_no_dynamic_or_unsafe_code_in_application_code():
    offenders = []

    for path in (REPO / "src").rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        for snippet in UNSAFE_CODE_SNIPPETS:
            if snippet in content:
                offenders.append(f"{path.name}: {snippet!r}")

    assert not offenders, f"unsafe constructs found: {offenders}"


def test_no_unsafe_yaml_loading_in_code():
    unsafe_markers = (
        "yaml.load(",
        "yaml.unsafe_load(",
        "yaml.full_load(",
        "yaml.Loader",
    )
    offenders = []

    for search_root in (REPO / "src", REPO / "Config"):
        for path in search_root.rglob("*.py"):
            content = path.read_text(encoding="utf-8")
            for marker in unsafe_markers:
                if marker in content:
                    offenders.append(f"{path.name}: {marker!r}")

    assert not offenders, f"unsafe YAML loading found: {offenders}"
    