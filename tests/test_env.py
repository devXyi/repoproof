from __future__ import annotations

from pathlib import Path

from repoproof.checks.env import (
    check_env,
    find_deployment_files,
    find_example_files,
    parse_deployment,
    parse_example,
    scan_code,
)
from repoproof.config import EnvConfig
from repoproof.markdown import parse_markdown

PYTHON_SOURCE = """\
import os
from os import environ, getenv

a = os.environ["A"]
b = os.environ.get("B")
c = os.environ.get("C", "x")
d = os.getenv("D")
e = os.getenv("E", "x")
f = environ["F"]
g = getenv("G")
os.environ["WRITTEN"] = "1"
h = os.environ[some_variable]
"""


def flags(refs):
    return {name: places[0].required for name, places in refs.items()}


def test_python_reads_are_found_with_required_flags(make_repo):
    root = make_repo({"app.py": PYTHON_SOURCE})
    refs = scan_code(root)
    assert flags(refs) == {"A": True, "B": False, "C": False, "D": False, "E": False, "F": True, "G": False}
    assert refs["A"][0].line == 4 and refs["A"][0].file == "app.py"


def test_python_files_that_do_not_parse_are_skipped(make_repo):
    root = make_repo({"broken.py": "def (:\n", "ok.py": 'import os\nos.environ["X"]\n'})
    assert list(scan_code(root)) == ["X"]


def test_javascript_reads(make_repo):
    root = make_repo(
        {
            "src/app.ts": (
                "const a = process.env.API_URL;\n"
                'const b = process.env["TOKEN"];\n'
                "const c = process.env.PORT || 3000;\n"
                'const d = process.env.HOST ?? "localhost";\n'
                'process.env.WRITTEN = "1";\n'
                "if (process.env.FLAG == 'x') {}\n"
                "const e = import.meta.env.VITE_KEY;\n"
                "const f = import.meta.env.MODE;\n"
                "// const g = process.env.COMMENTED;\n"
            )
        }
    )
    assert flags(scan_code(root)) == {
        "API_URL": True, "TOKEN": True, "PORT": False, "HOST": False, "FLAG": True, "VITE_KEY": True,
    }  # fmt: skip


def test_go_reads(make_repo):
    root = make_repo(
        {
            "main.go": (
                'a := os.Getenv("A")\n'
                'b, ok := os.LookupEnv("B")\n'
                'if os.Getenv("C") == "" {\n}\n'
            )
        }
    )
    assert flags(scan_code(root)) == {"A": True, "B": False, "C": False}


def test_rust_reads(make_repo):
    root = make_repo(
        {
            "src/main.rs": (
                'let a = env::var("A").unwrap();\n'
                'let b = std::env::var("B").unwrap_or_default();\n'
                'let c = env!("C");\n'
                'let d = option_env!("D");\n'
                'let e = env::var("E").ok();\n'
            )
        }
    )
    assert flags(scan_code(root)) == {"A": True, "B": False, "C": True, "D": False, "E": False}


def test_tests_and_vendored_code_are_not_scanned_by_default(make_repo):
    source = 'import os\nos.environ["X"]\n'
    root = make_repo(
        {
            "app.py": source.replace("X", "REAL"),
            "tests/test_a.py": source.replace("X", "IN_TESTS"),
            "pkg/test_b.py": source.replace("X", "TEST_FILE"),
            "pkg/conftest.py": source.replace("X", "CONFTEST"),
            "node_modules/dep/index.js": "process.env.VENDORED",
            "venv/lib/x.py": source.replace("X", "VENV"),
            "examples/demo.py": source.replace("X", "EXAMPLE"),
        }
    )
    assert sorted(scan_code(root)) == ["EXAMPLE", "REAL"]
    assert "IN_TESTS" in scan_code(root, include_tests=True)
    assert sorted(scan_code(root, exclude=("examples/*",))) == ["REAL"]


def test_parse_example_files():
    text = "A=1\n# B=2\nexport C=3\n# just a comment\n D = 4\nnot a var\n"
    assert parse_example(text) == {"A": 1, "B": 2, "C": 3, "D": 5}


def test_parse_compose_environment_blocks():
    text = """\
services:
  web:
    image: x
    env_file: .env
    environment:
      DATABASE_URL: postgres://db
      DEBUG: "1"
      lower_case: ignored
    ports: ["${PORT:-8000}:8000"]
  worker:
    environment:
      - API_TOKEN=abc
      - REDIS_URL
"""
    names, env_file = parse_deployment(text, "docker-compose.yml")
    assert env_file
    assert set(names) == {"DATABASE_URL", "DEBUG", "PORT", "API_TOKEN", "REDIS_URL"}
    assert names["API_TOKEN"] == 12


def test_parse_kubernetes_env_and_dockerfile():
    manifest = """\
containers:
  - name: app
    env:
      - name: DB_HOST
        value: x
      - name: SECRET
        valueFrom:
          secretKeyRef:
            name: creds
            key: password
"""
    names, _ = parse_deployment(manifest, "deployment.yaml")
    assert set(names) == {"DB_HOST", "SECRET"}
    dockerfile = "FROM python\nENV A=1 B=2\nENV C 3\nARG BUILD_ID\nRUN echo $NOPE\n"
    names, env_file = parse_deployment(dockerfile, "Dockerfile")
    assert set(names) == {"A", "B", "C", "BUILD_ID"} and not env_file


def test_file_discovery(make_repo):
    root = make_repo(
        {
            ".env.example": "", ".env.sample": "", "Dockerfile": "", "docker-compose.dev.yml": "",
            "k8s/app.yaml": "", "k8s/notes.txt": "", "app.yaml": "", "config/extra.env": "",
        }
    )  # fmt: skip
    assert find_example_files(root, ["config/extra.env", "missing.env"]) == [
        ".env.example", ".env.sample", "config/extra.env",
    ]  # fmt: skip
    assert find_deployment_files(root, []) == ["Dockerfile", "docker-compose.dev.yml", "k8s/app.yaml"]


def check(root: Path, config: EnvConfig | None = None, readme: str = "README.md"):
    docs = {readme: parse_markdown((root / readme).read_text(encoding="utf-8"), readme)}
    return check_env(root, docs, config)


def by_rule(findings):
    out: dict[str, list[str]] = {}
    for finding in findings:
        out.setdefault(finding.rule_id, []).append(finding.message.split("`")[1])
    return {rule: sorted(names) for rule, names in out.items()}


def test_findings_across_code_docs_example_and_deployment(make_repo):
    root = make_repo(
        {
            "app.py": (
                "import os\n"
                'DATABASE_URL = os.environ["DATABASE_URL"]\n'
                'API_TOKEN = os.environ["API_TOKEN"]\n'
                'SECRET_KEY = os.environ["SECRET_KEY"]\n'
                'DEBUG = os.getenv("DEBUG", "0")\n'
                'HOME = os.environ["HOME"]\n'
            ),
            "README.md": "# App\nSet `DATABASE_URL` and `SECRET_KEY` before starting.\n",
            ".env.example": "DATABASE_URL=sqlite:///x\nOLD_FLAG=1\n",
            "docker-compose.yml": "services:\n  web:\n    environment:\n      DATABASE_URL: x\n",
        }
    )
    findings = check(root)
    assert by_rule(findings) == {
        "RP301": ["API_TOKEN", "DEBUG"],
        "RP302": ["SECRET_KEY"],
        "RP303": ["OLD_FLAG"],
        "RP304": ["API_TOKEN", "SECRET_KEY"],
    }
    severity = {(f.rule_id, f.message.split("`")[1]): f.level.value for f in findings}
    assert severity[("RP301", "API_TOKEN")] == "warning"  # required, no default
    assert severity[("RP301", "DEBUG")] == "note"  # has a default
    assert severity[("RP302", "SECRET_KEY")] == "warning"
    assert severity[("RP303", "OLD_FLAG")] == "note"
    token = next(f for f in findings if f.rule_id == "RP301" and "API_TOKEN" in f.message)
    assert (token.file, token.line) == ("app.py", 3)
    stale = next(f for f in findings if f.rule_id == "RP303")
    assert (stale.file, stale.line) == (".env.example", 2)


def test_ignore_patterns_and_defaults(make_repo):
    root = make_repo(
        {
            "app.py": 'import os\nos.environ["API_TOKEN"]\nos.environ["HOME"]\nos.environ["PYTHONPATH"]\nos.environ["INTERNAL_X"]\n',
            "README.md": "# App\n",
        }
    )
    assert by_rule(check(root, EnvConfig(ignore=["INTERNAL_*"]))) == {"RP301": ["API_TOKEN"]}


def test_env_file_in_compose_means_the_example_provides_the_values(make_repo):
    root = make_repo(
        {
            "app.py": 'import os\nos.environ["A"]\n',
            "README.md": "Set `A`.\n",
            ".env.example": "A=1\n",
            "docker-compose.yml": "services:\n  web:\n    env_file: .env\n",
        }
    )
    assert check(root) == []


def test_a_repo_with_no_environment_reads_has_no_findings(make_repo):
    root = make_repo({"README.md": "# Nothing\n", "main.py": "print('hi')\n"})
    assert check(root) == []


def test_undocumented_variables_defined_by_deployment_say_so(make_repo):
    root = make_repo(
        {
            "app.py": 'import os\nos.environ["A"]\n',
            "README.md": "# App\n",
            "Dockerfile": "FROM python\nENV A=1\n",
        }
    )
    (finding,) = check(root)
    assert finding.rule_id == "RP301" and "It is set in Dockerfile" in finding.message
