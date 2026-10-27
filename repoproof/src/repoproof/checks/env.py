"""Configuration consistency: environment variables in code versus what is documented.

Variables read by the code (Python via ``ast``; JavaScript/TypeScript, Go and Rust via
patterns) are compared with three places a newcomer would look:

* the documentation files (a variable counts as documented if it appears there as a word),
* ``.env.example`` and friends,
* deployment configuration (docker-compose files, Dockerfiles, Kubernetes manifests).

The check is heuristic by nature. Findings say what was seen and where; the rules and their
severities are documented in docs/checks.md.
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import EnvConfig
from ..markdown import MarkdownDoc
from ..models import Finding, Severity

MAX_FILE_BYTES = 1_000_000
LANGUAGES = {
    ".py": "python",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".vue": "js", ".svelte": "js",
    ".go": "go",
    ".rs": "rust",
}  # fmt: skip
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages", "vendor",
        ".next", ".nuxt", "coverage", "htmlcov", ".idea", ".vscode",
    }
)  # fmt: skip
TEST_DIRS = frozenset({"tests", "test", "__tests__", "spec", "e2e"})
EXAMPLE_FILES = (
    ".env.example", ".env.sample", ".env.template", ".env.dist", "example.env", "sample.env",
    "env.example", ".env.local.example", ".env.development.example",
)  # fmt: skip
DEPLOY_DIRS = ("k8s", "kubernetes", "deploy", "deployment", "charts", "helm", "manifests")
DEFAULT_IGNORE = (
    "PATH", "HOME", "USER", "USERNAME", "LOGNAME", "SHELL", "PWD", "OLDPWD", "TERM", "LANG",
    "LANGUAGE", "LC_*", "TZ", "TMPDIR", "TMP", "TEMP", "HOSTNAME", "EDITOR", "VISUAL", "PAGER",
    "DISPLAY", "CI", "NO_COLOR", "FORCE_COLOR", "COLORTERM", "CLICOLOR", "CLICOLOR_FORCE",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHON*", "PIP_*", "NODE_ENV", "NODE_OPTIONS", "NODE_PATH",
    "GITHUB_*", "RUNNER_*", "XDG_*", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "PROGRAMFILES*",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "KUBECONFIG",
    "SOURCE_DATE_EPOCH", "REPOPROOF_*",
)  # fmt: skip
_VITE_BUILTINS = frozenset({"MODE", "DEV", "PROD", "SSR", "BASE_URL"})

_NAME = r"([A-Za-z_][A-Za-z0-9_]*)"
_JS_PATTERNS = (
    re.compile(r"process\.env\." + _NAME),
    re.compile(r"""process\.env\[\s*['"]""" + _NAME + r"""['"]\s*\]"""),
    re.compile(r"import\.meta\.env\." + _NAME),
)
_GO_PATTERNS = (
    ("Getenv", re.compile(r'os\.Getenv\(\s*"' + _NAME + r'"\s*\)')),
    ("LookupEnv", re.compile(r'os\.LookupEnv\(\s*"' + _NAME + r'"\s*\)')),
)
_RUST_VAR = re.compile(r'(?:std::)?env::var(?:_os)?\(\s*"' + _NAME + r'"\s*\)')
_RUST_MACRO = re.compile(r'\b(option_env|env)!\(\s*"' + _NAME + r'"')
_RUST_OPTIONAL = re.compile(r"\s*\.(?:unwrap_or\w*|ok\(\)|is_ok\(\)|is_err\(\))")
_EXAMPLE_LINE = re.compile(r"^\s*#?\s*(?:export\s+)?" + _NAME + r"\s*=")
_UPPER = r"([A-Z_][A-Z0-9_]*)"
_ENV_BLOCK = re.compile(r"^(\s*)(?:environment|env)\s*:\s*(?:#.*)?$")
_INTERPOLATION = re.compile(r"\$\{" + _NAME)
_BLOCK_ITEMS = (
    re.compile(r"^\s*-?\s*name\s*:\s*['\"]?" + _UPPER + r"['\"]?\s*(?:#.*)?$"),
    re.compile(r"^\s*-\s*['\"]?" + _UPPER + r"(?:=|['\"]?\s*(?:#.*)?$)"),
    re.compile(r"^\s*['\"]?" + _UPPER + r"['\"]?\s*:"),
)


@dataclass(frozen=True)
class EnvRef:
    """One place where the code reads an environment variable."""

    name: str
    file: str
    line: int
    required: bool


# -- code scanning ---------------------------------------------------------------------


def scan_code(
    root: Path, include_tests: bool = False, exclude: tuple[str, ...] = ()
) -> dict[str, list[EnvRef]]:
    """Map variable name to every place the code reads it."""
    refs: dict[str, list[EnvRef]] = {}
    for relative in _source_files(root, include_tests):
        if any(fnmatch.fnmatchcase(relative, pattern) for pattern in exclude):
            continue
        path = root / relative
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        language = LANGUAGES[path.suffix.lower()]
        found = _scan_python(source, relative) if language == "python" else _scan_text(language, source, relative)
        for ref in found:
            refs.setdefault(ref.name, []).append(ref)
    return refs


def _source_files(root: Path, include_tests: bool) -> Iterator[str]:
    rust = (root / "Cargo.toml").exists()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not d.endswith(".egg-info")
            and (include_tests or d not in TEST_DIRS)
            and not (rust and d == "target")
        )
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() not in LANGUAGES:
                continue
            if not include_tests and _is_test_file(name):
                continue
            yield Path(dirpath, name).relative_to(root).as_posix()


def _is_test_file(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("test_")
        or lowered.endswith(("_test.py", "_test.go", "_test.rs"))
        or lowered == "conftest.py"
        or ".test." in lowered
        or ".spec." in lowered
    )


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_environ(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "environ"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_getenv(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "getenv"
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getenv"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _scan_python(source: str, relative: str) -> list[EnvRef]:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    refs: list[EnvRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if _is_environ(node.value):
                key = _const_str(node.slice)
                if key:
                    refs.append(EnvRef(key, relative, node.lineno, True))
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            reads = isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value)
            if reads or _is_getenv(func):
                key = _const_str(node.args[0])
                if key:
                    refs.append(EnvRef(key, relative, node.lineno, False))
    return sorted(refs, key=lambda r: (r.line, r.name))


def _scan_text(language: str, source: str, relative: str) -> list[EnvRef]:
    refs: list[EnvRef] = []
    for number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("//", "/*", "*")):
            continue
        if language == "js":
            for pattern in _JS_PATTERNS:
                for match in pattern.finditer(line):
                    name, rest = match.group(1), line[match.end() :]
                    if name in _VITE_BUILTINS and "import.meta" in match.group(0):
                        continue
                    if re.match(r"\s*=(?!=)", rest):
                        continue  # an assignment, not a read
                    optional = re.match(r"\s*(?:\|\||\?\?)", rest) is not None
                    refs.append(EnvRef(name, relative, number, not optional))
        elif language == "go":
            for kind, pattern in _GO_PATTERNS:
                for match in pattern.finditer(line):
                    checked = re.match(r"\s*(?:==|!=)", line[match.end() :]) is not None
                    refs.append(EnvRef(match.group(1), relative, number, kind == "Getenv" and not checked))
        elif language == "rust":
            for match in _RUST_VAR.finditer(line):
                optional = _RUST_OPTIONAL.match(line[match.end() :]) is not None
                refs.append(EnvRef(match.group(1), relative, number, not optional))
            for match in _RUST_MACRO.finditer(line):
                refs.append(EnvRef(match.group(2), relative, number, match.group(1) == "env"))
    return refs


# -- documentation, examples and deployment ----------------------------------------------


def parse_example(text: str) -> dict[str, int]:
    """Variables listed in an ``.env.example``-style file (commented entries count)."""
    names: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _EXAMPLE_LINE.match(line)
        if match:
            names.setdefault(match.group(1), number)
    return names


def parse_deployment(text: str, filename: str) -> tuple[dict[str, int], bool]:
    """Variables a deployment file sets, and whether it loads an ``env_file``."""
    names: dict[str, int] = {}
    lines = text.splitlines()
    lowered = filename.lower()
    if lowered.startswith("dockerfile") or lowered.endswith(".dockerfile"):
        for number, line in enumerate(lines, start=1):
            match = re.match(r"^\s*(ENV|ARG)\s+(.*)$", line, re.IGNORECASE)
            if not match:
                continue
            body = match.group(2).strip()
            if match.group(1).upper() == "ENV" and "=" in body.split()[0]:
                found = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=", body)
            else:
                first = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", body)
                found = [first.group(1)] if first else []
            for name in found:
                names.setdefault(name, number)
        return names, False

    uses_env_file = False
    for number, line in enumerate(lines, start=1):
        for match in _INTERPOLATION.finditer(line):
            names.setdefault(match.group(1), number)
        if re.match(r"^\s*-?\s*env_file\s*:", line) or re.match(r"^\s*-?\s*envFrom\s*:", line):
            uses_env_file = True
    for index, line in enumerate(lines):
        block = _ENV_BLOCK.match(line)
        if not block:
            continue
        indent = len(block.group(1))
        for offset in range(index + 1, len(lines)):
            inner = lines[offset]
            stripped = inner.strip()
            if not stripped or stripped.startswith("#"):
                continue
            inner_indent = len(inner) - len(inner.lstrip())
            if inner_indent < indent or (inner_indent == indent and not stripped.startswith("-")):
                break
            for pattern in _BLOCK_ITEMS:
                found = pattern.match(inner)
                if found:
                    names.setdefault(found.group(1), offset + 1)
                    break
    return names, uses_env_file


def find_example_files(root: Path, extra: list[str]) -> list[str]:
    found = [name for name in EXAMPLE_FILES if (root / name).is_file()]
    found += [name for name in extra if (root / name).is_file() and name not in found]
    return found


def find_deployment_files(root: Path, extra: list[str]) -> list[str]:
    found: list[str] = []
    try:
        top = sorted(os.listdir(root))
    except OSError:
        return found
    for name in top:
        lowered = name.lower()
        if not (root / name).is_file():
            continue
        compose = lowered.startswith(("docker-compose", "compose")) and lowered.endswith((".yml", ".yaml"))
        if compose or lowered.startswith("dockerfile") or lowered.endswith(".dockerfile"):
            found.append(name)
    for directory in DEPLOY_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                if name.lower().endswith((".yml", ".yaml")):
                    found.append(Path(dirpath, name).relative_to(root).as_posix())
    found += [name for name in extra if (root / name).is_file() and name not in found]
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _ignored(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _join_or(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


def _place(ref: EnvRef, total: int) -> str:
    more = f" (and {total - 1} more place{'s' if total > 2 else ''})" if total > 1 else ""
    return f"{ref.file}:{ref.line}{more}"


def check_env(
    root: Path, docs: Mapping[str, MarkdownDoc], config: EnvConfig | None = None
) -> list[Finding]:
    """Compare the variables the code reads with its documentation and deployment config."""
    config = config or EnvConfig()
    ignore = [*DEFAULT_IGNORE, *config.ignore]
    refs = {
        name: places
        for name, places in scan_code(root, config.include_tests, tuple(config.exclude)).items()
        if not _ignored(name, ignore)
    }

    doc_words: set[str] = set()
    for doc in docs.values():
        doc_words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", doc.text))
    example_files = find_example_files(root, config.examples)
    examples: dict[str, tuple[str, int]] = {}
    for relative in example_files:
        for name, number in parse_example(_read(root / relative)).items():
            examples.setdefault(name, (relative, number))

    deploy_files = find_deployment_files(root, config.deployment)
    deploy: dict[str, tuple[str, int]] = {}
    loads_env_file = False
    for relative in deploy_files:
        names, env_file = parse_deployment(_read(root / relative), os.path.basename(relative))
        loads_env_file = loads_env_file or env_file
        for name, number in names.items():
            deploy.setdefault(name, (relative, number))

    findings: list[Finding] = []
    labels = [*docs, *example_files]
    where = _join_or(labels) if labels else "any documentation"
    documented = doc_words | set(examples)
    for name in sorted(refs):
        places = refs[name]
        first = places[0]
        required = any(p.required for p in places)
        if name not in documented:
            note = f" It is set in {deploy[name][0]}." if name in deploy else ""
            findings.append(
                Finding(
                    "RP301",
                    f"`{name}` is read at {_place(first, len(places))} but is not documented in {where}.{note}",
                    severity=None if required else Severity.NOTE,
                    file=first.file,
                    line=first.line,
                )
            )
        elif required and example_files and name in doc_words and name not in examples:
            findings.append(
                Finding(
                    "RP302",
                    f"`{name}` is required at {_place(first, len(places))} and documented, "
                    f"but missing from {', '.join(example_files)}.",
                    file=first.file,
                    line=first.line,
                )
            )
    for name, (relative, number) in sorted(examples.items()):
        if name in refs or name in deploy or _ignored(name, ignore):
            continue
        findings.append(
            Finding(
                "RP303",
                f"`{name}` is listed in {relative} but is never read by the code or deployment config.",
                file=relative,
                line=number,
            )
        )
    if deploy or loads_env_file:
        provided = set(deploy) | (set(examples) if loads_env_file else set())
        for name in sorted(refs):
            places = refs[name]
            if name in provided or not any(p.required for p in places):
                continue
            findings.append(
                Finding(
                    "RP304",
                    f"`{name}` is required at {_place(places[0], len(places))} "
                    f"but is not set in {', '.join(deploy_files) or 'the deployment config'}.",
                    file=places[0].file,
                    line=places[0].line,
                )
            )
    return findings
