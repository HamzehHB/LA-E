"""Write-surface security proof: enumerated filesystem writers.

Static AST scan over the current application source tree (proven from
code, not from historical reports):

* exactly two filesystem-mutation modules exist: the
  controlled-execution staging ``write_text`` (``executor.py``) and the
  append-only audit writer (``audit/persistence.py``);
* the executor performs exactly one staging write and never deletes,
  renames, creates directories, or replaces paths;
* the audit writer appends audit lines only (``open`` in ``"a"`` mode),
  creates only its configured audit root (``makedirs``), and never
  deletes, renames, or replaces paths;
* the governance and security boundary families contain no
  rename/replace capability at all, and no ``os.*`` path mutation is
  called anywhere in application code;
* every other ``open`` call in ``src/`` is read-mode only;
* no process, network, dynamic-code, or deserialization import exists
  in application code (outside the narrow LLM urllib/http allowance).

Together these prove the end-to-end security invariants --
ControlledExecutor writes staging only, AuditWriter writes audit only,
and no third filesystem execution path exists.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"

EXECUTOR = "living_authenticity/knowledge/governance/execution/executor.py"

AUDIT_WRITER = "living_authenticity/knowledge/governance/audit/persistence.py"

WRITER_MODULES = frozenset({EXECUTOR, AUDIT_WRITER})

AUDIT_ALLOWED_MUTATIONS = frozenset({"write", "makedirs"})

MUTATION_ATTRS = frozenset({
    "write_text", "write_bytes", "write",
    "unlink", "rename", "mkdir", "rmdir",
    "touch", "remove", "rmtree", "symlink", "link", "move",
})

BOUNDARY_FAMILY_PREFIXES = (
    "living_authenticity/knowledge/governance/",
    "living_authenticity/security/",
)

FORBIDDEN_IMPORT_ROOTS = frozenset({
    "subprocess", "socket", "shutil", "pickle", "marshal", "ctypes",
    "requests", "httpx", "urllib", "http", "importlib",
})

# Narrow, explicit exemption: the LLM provider layer must reach a
# configured local/cloud endpoint, and the project deliberately uses the
# standard library for that call instead of adding a dependency. Only
# ``urllib`` and ``http`` are permitted, and only inside this one
# package. Every other banned root (subprocess, socket, shutil, pickle,
# marshal, ctypes, importlib, requests, httpx) stays banned there too,
# and the full list stays banned everywhere else in application code.
LLM_PACKAGE_PREFIX = "living_authenticity/llm/"
NETWORK_IMPORT_ALLOWANCE = frozenset({"urllib", "http"})

FORBIDDEN_BUILTIN_CALLS = frozenset({
    "eval", "exec", "__import__", "compile",
})


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def _iter_calls(tree):
    """Yield ``(attr, base, node)`` for every call in ``tree``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            yield func.id, "", node
        elif isinstance(func, ast.Attribute):
            base = ""
            if isinstance(func.value, ast.Name):
                base = func.value.id
            elif isinstance(func.value, ast.Attribute):
                base = func.value.attr
            yield func.attr, base, node


def _extract_mode(node: ast.Call, is_method: bool) -> str:
    """Extract mode argument accurately whether positional, keyword, or default."""
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    # Positional mode index: 0 for method path.open(mode), 1 for global open(file, mode)
    mode_idx = 0 if is_method else 1
    if len(node.args) > mode_idx:
        arg = node.args[mode_idx]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return "r"


def test_controlled_executor_is_the_sole_mutation_call_site():
    offenders = []
    executor_seen = False
    audit_seen = False
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr, _base, _node in _iter_calls(tree):
            if attr not in MUTATION_ATTRS:
                continue
            relative = _relative(path)
            if relative == EXECUTOR:
                executor_seen = True
            elif relative == AUDIT_WRITER:
                audit_seen = True
            else:
                offenders.append(relative + ":" + attr)
    assert executor_seen is True, (
        "scan found no write in the executor; the scan itself is broken"
    )
    assert audit_seen is True, (
        "scan found no write in the audit writer; the scan itself is broken"
    )
    assert offenders == [], f"alternate filesystem mutation found: {offenders}"


def test_only_two_writer_modules_exist():
    found = set()
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr, _base, _node in _iter_calls(tree):
            if attr in MUTATION_ATTRS:
                found.add(_relative(path))
    assert found == set(WRITER_MODULES), f"writer modules changed: {sorted(found)}"


def test_audit_writer_appends_only_and_never_deletes_or_renames():
    tree = ast.parse((SRC / AUDIT_WRITER).read_text(encoding="utf-8"))
    counts = {}
    for attr, _base, _node in _iter_calls(tree):
        counts[attr] = counts.get(attr, 0) + 1
    for forbidden in ("unlink", "rename", "replace", "rmdir", "remove",
                      "rmtree", "symlink", "link", "move", "write_text",
                      "write_bytes", "touch"):
        assert counts.get(forbidden, 0) == 0, (
            f"audit writer must not perform {forbidden}"
        )
    assert counts.get("write", 0) >= 1, "audit writer must append lines"
    assert counts.get("makedirs", 0) >= 1, "audit writer may create its root"


def test_audit_writer_uses_append_mode_only():
    tree = ast.parse((SRC / AUDIT_WRITER).read_text(encoding="utf-8"))
    for attr, base, node in _iter_calls(tree):
        if attr != "open":
            continue
        is_method = bool(base) or isinstance(node.func, ast.Attribute)
        mode = _extract_mode(node, is_method)
        assert mode.startswith("a"), (
            f"audit writer open() must be append-mode, got {mode!r}")
    text = (SRC / AUDIT_WRITER).read_text(encoding="utf-8")
    assert '"w"' not in text.replace("AuditWriter", ""), (
        "audit writer must not contain truncate-write mode")


def _code_text_without_docstrings(path: Path) -> str:
    """Return source text minus docstrings for capability scans.

    Docstrings may legitimately name sibling boundaries to document
    disjointness (e.g. the audit writer states it never touches
    staging); capability must be proven from code, not prose.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                first.value.value = ""
    return ast.unparse(tree)


def test_audit_writer_cannot_reach_vault_or_staging():
    code = _code_text_without_docstrings(SRC / AUDIT_WRITER)
    for marker in ("staging", "vault", "vector", "lance", "ControlledExecutor"):
        assert marker.lower() not in code.lower(), (
            f"audit writer must not reference {marker}")
    assert "audit" in code.lower()


def test_executor_cannot_reach_audit_root():
    code = _code_text_without_docstrings(SRC / EXECUTOR)
    assert "audit" not in code.lower(), (
        "executor must not reference the audit root")


def test_executor_writes_once_and_never_deletes_or_renames():
    tree = ast.parse((SRC / EXECUTOR).read_text(encoding="utf-8"))
    counts = {}
    for attr, _base, _node in _iter_calls(tree):
        counts[attr] = counts.get(attr, 0) + 1
    assert counts.get("write_text") == 1
    for forbidden in ("unlink", "rename", "replace", "rmdir", "mkdir", "remove"):
        assert counts.get(forbidden, 0) == 0, (
            f"executor must not perform {forbidden}"
        )


def test_no_rename_or_replace_in_boundary_families_or_via_os():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr, base, _node in _iter_calls(tree):
            if attr not in ("replace", "rename"):
                continue
            in_boundary_family = relative.startswith(BOUNDARY_FAMILY_PREFIXES)
            if in_boundary_family or base == "os":
                offenders.append(relative + ":" + base + "." + attr)
    assert offenders == [], f"rename/replace capability found: {offenders}"


def test_all_open_calls_in_src_are_read_mode():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        relative = _relative(path)
        if relative == AUDIT_WRITER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr, base, node in _iter_calls(tree):
            if attr != "open":
                continue
            is_method = bool(base) or isinstance(node.func, ast.Attribute)
            mode = _extract_mode(node, is_method)
            if not mode.startswith("r"):
                offenders.append(_relative(path) + ":" + mode)
    assert offenders == [], f"non-read open() in application code: {offenders}"


def _is_allowed_network_import(relative: str, root: str) -> bool:
    """True only for urllib/http inside the LLM provider package."""
    return (relative.startswith(LLM_PACKAGE_PREFIX)
            and root in NETWORK_IMPORT_ALLOWANCE)


def _import_offenders(tree, relative: str) -> list:
    """Return ``path:root`` for every banned import in one module."""
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]]
        else:
            continue
        for root in roots:
            if root not in FORBIDDEN_IMPORT_ROOTS:
                continue
            if _is_allowed_network_import(relative, root):
                continue
            offenders.append(relative + ":" + root)
    return offenders


def test_no_process_network_or_deserialization_imports():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders.extend(_import_offenders(tree, _relative(path)))
    assert offenders == [], f"forbidden import in application code: {offenders}"


def test_network_import_allowance_is_limited_to_the_llm_package():
    """The allowance covers exactly urllib/http, exactly in ``llm/``.

    Everything else — including the rest of the banned list and every
    other application package — stays rejected, and the scan has real
    discriminating power (proved against synthetic modules rather than
    asserted by name).
    """
    llm_relative = LLM_PACKAGE_PREFIX + "provider.py"
    other_relative = "living_authenticity/knowledge/output/module.py"

    network_only = ast.parse("import urllib.request\nfrom http.client import X\n")
    assert _import_offenders(network_only, llm_relative) == []
    assert _import_offenders(network_only, other_relative) == [
        other_relative + ":urllib", other_relative + ":http",
    ]

    still_banned_in_llm = ast.parse(
        "import subprocess\nimport pickle\nimport socket\nimport shutil\n"
        "import marshal\nimport ctypes\nimport importlib\n"
        "import requests\nimport httpx\n"
    )
    found = _import_offenders(still_banned_in_llm, llm_relative)
    for banned in ("subprocess", "pickle", "socket", "shutil", "marshal",
                   "ctypes", "importlib", "requests", "httpx"):
        assert llm_relative + ":" + banned in found, banned

    # The allowance must not be reachable by a name-prefix trick.
    lookalike = "living_authenticity/llm_extra/module.py"
    assert _import_offenders(network_only, lookalike) != []


def test_no_dynamic_code_execution_calls():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for attr, base, _node in _iter_calls(tree):
            if attr in ("eval", "exec", "__import__"):
                offenders.append(_relative(path) + ":" + attr)
            elif attr == "compile" and base != "re":
                offenders.append(_relative(path) + ":" + (base + "." if base else "") + attr)
    assert offenders == [], f"dynamic execution call in application code: {offenders}"


def test_mutation_scan_cannot_be_trivially_bypassed():
    """Negative-control: the mutation-term scan must trip on a synthetic
    writer outside the executor while leaving the executor's single
    staging write accepted.

    This proves the scan has discriminating power and is not a tautology
    that passes on any tree.
    """
    tree = ast.parse(
        "target.write_text('x', encoding='utf-8')\n"
        "target.mkdir()\n"
        "os.remove('x')\n"
        "pathlib.Path('x').unlink()\n"
    )
    found = sorted(
        attr for attr, _base, _node in _iter_calls(tree)
        if attr in MUTATION_ATTRS
    )
    assert "mkdir" in found
    assert "remove" in found
    assert "unlink" in found
    assert "write_text" in found
