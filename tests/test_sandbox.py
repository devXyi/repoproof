from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from repoproof import sandbox as sandbox_module
from repoproof.config import SandboxConfig
from repoproof.sandbox import (
    IsolationError,
    RedirectSpec,
    Sandbox,
    build_base_env,
    clean_path,
    copy_repo,
    network_namespace_support,
    run_process,
)

HAS_GIT = shutil.which("git") is not None
CAN_UNSHARE = network_namespace_support()[0] is not None


def listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def test_copy_skips_caches_virtualenvs_and_vcs_data(make_repo, tmp_path):
    root = make_repo(
        {
            "README.md": "x", "src/a.py": "x", ".venv/bin/python": "x", "venv/y": "x",
            "node_modules/z/index.js": "x", "pkg/__pycache__/c.pyc": "x", "pkg.egg-info/PKG-INFO": "x",
            ".git/config": "x", "dist/old.whl": "x", ".env.example": "x",
        }
    )  # fmt: skip
    destination = tmp_path / "copy"
    copy_repo(root, destination)
    assert listing(destination) == {"README.md", "src/a.py", ".env.example"}


def test_copy_preserves_symlinks_instead_of_following_them(make_repo, tmp_path):
    root = make_repo({"real.txt": "x"})
    os.symlink("real.txt", root / "alias.txt")
    destination = tmp_path / "copy"
    copy_repo(root, destination)
    assert (destination / "alias.txt").is_symlink()


@pytest.mark.skipif(not HAS_GIT, reason="git is not installed")
def test_copy_in_a_git_checkout_matches_a_clone_plus_new_files(make_repo, tmp_path):
    root = make_repo(
        {
            ".gitignore": "secret.txt\nbuild/\n", "tracked.txt": "t", "gone.txt": "g",
            "secret.txt": "s", "build/out.bin": "b", "untracked.txt": "u",
        }
    )  # fmt: skip
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q")
    git("add", ".gitignore", "tracked.txt", "gone.txt")
    (root / "gone.txt").unlink()  # indexed but deleted from the working tree

    full = tmp_path / "full"
    copy_repo(root, full)
    assert listing(full) == {".gitignore", "tracked.txt", "untracked.txt"}

    only_tracked = tmp_path / "tracked"
    copy_repo(root, only_tracked, tracked_only=True)
    assert listing(only_tracked) == {".gitignore", "tracked.txt"}


def test_base_environment_inherits_nothing_by_accident():
    host = {
        "PATH": "/usr/bin:/venv/bin:/bin", "VIRTUAL_ENV": "/venv", "HOME": "/home/real",
        "AWS_SECRET_ACCESS_KEY": "aws", "GITHUB_TOKEN": "gh", "CUSTOM": "yes",
        "LANG": "de_DE.UTF-8", "LC_ALL": "C", "HTTPS_PROXY": "http://proxy:3128",
    }  # fmt: skip
    env = build_base_env(host, home="/sb/home", tmp="/sb/tmp")
    assert env["HOME"] == "/sb/home" and env["TMPDIR"] == "/sb/tmp"
    assert env["PATH"] == "/usr/bin:/bin"  # the host's active virtualenv is gone
    assert env["LANG"] == "de_DE.UTF-8" and env["LC_ALL"] == "C"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["REPOPROOF_ACTIVE"] == "1" and env["NO_COLOR"] == "1"
    for leaked in ("AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "CUSTOM", "VIRTUAL_ENV"):
        assert leaked not in env
    assert "HTTPS_PROXY" not in build_base_env(host, home="/h", tmp="/t", inherit_proxy=False)
    assert build_base_env(host, home="/h", tmp="/t", pass_env=["CUSTOM", "ABSENT"])["CUSTOM"] == "yes"


def test_clean_path_drops_only_the_active_environments():
    host = {"VIRTUAL_ENV": "/p/.venv", "CONDA_PREFIX": "/opt/conda/envs/x"}
    path = "/p/.venv/bin:/usr/bin:/opt/conda/envs/x/bin:/bin"
    assert clean_path(path, host) == "/usr/bin:/bin"
    assert clean_path("/usr/bin", {}) == "/usr/bin"


def run(code: str, cwd: Path, *, timeout: float = 20, max_bytes: int = 65536, redirects=None):
    return run_process(
        [sys.executable, "-c", code], cwd=str(cwd), env=os.environ, timeout=timeout,
        max_bytes=max_bytes, redirects=redirects,
    )  # fmt: skip


def test_exit_status_and_merged_output(tmp_path):
    result = run("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)", tmp_path)
    assert result.exit_code == 3 and not result.timed_out
    assert "out" in result.output and "err" in result.output


def test_only_the_tail_of_huge_output_is_kept(tmp_path):
    result = run("for i in range(20000): print('line', i)", tmp_path, max_bytes=1024)
    assert result.truncated and "truncated" in result.output
    assert len(result.output) < 1300 and result.output.rstrip().endswith("line 19999")


def test_timeout_kills_the_whole_process_group(tmp_path):
    code = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time, pathlib; time.sleep(1.5); "
        "pathlib.Path(\"marker\").write_text(\"x\")'])\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    result = run(code, tmp_path, timeout=0.5)
    assert result.timed_out and time.monotonic() - started < 10
    time.sleep(2.2)
    assert not (tmp_path / "marker").exists(), "a child outlived the timeout"


def test_redirects(tmp_path):
    out = tmp_path / "out.txt"
    code = "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr)"
    # stdout to a file, stderr captured
    result = run(code, tmp_path, redirects=RedirectSpec(("file", str(out), False), "capture"))
    assert out.read_text().strip() == "to-stdout" and result.output.strip() == "to-stderr"
    # both to the same appended file
    both = ("file", str(out), True)
    run(code, tmp_path, redirects=RedirectSpec(both, both))
    assert out.read_text().splitlines() == ["to-stdout", "to-stdout", "to-stderr"]
    # discarded
    quiet = run(code, tmp_path, redirects=RedirectSpec("null", "null"))
    assert quiet.output == "" and quiet.exit_code == 0


def test_a_missing_executable_is_reported_not_raised(tmp_path):
    result = run_process(["/nonexistent/tool"], cwd=str(tmp_path), env={}, timeout=5, max_bytes=1024)
    assert result.exit_code == 127 and "nonexistent" in result.output


def test_workspace_layout_and_cleanup(make_repo):
    repo = make_repo({"README.md": "hello"})
    with Sandbox(repo, SandboxConfig()) as box:
        root = box.root
        assert (box.work / "README.md").read_text() == "hello"
        assert box.base_env["HOME"] == str(box.home) and str(box.home).startswith(str(root))
        assert box.work.parent == root
    assert not root.exists()
    with Sandbox(repo, SandboxConfig(), keep=True) as kept:
        kept_root = kept.root
    assert kept_root.exists()
    shutil.rmtree(kept_root)


@pytest.mark.skipif(not CAN_UNSHARE, reason="network namespaces are unavailable here")
def test_network_namespace_cuts_off_the_network(make_repo):
    repo = make_repo({"probe.py": (
        "import socket, sys\n"
        "try:\n"
        "    socket.create_connection(('93.184.216.34', 80), timeout=3)\n"
        "except OSError as exc:\n"
        "    print('blocked:', exc); sys.exit(0)\n"
        "print('connected'); sys.exit(1)\n"
    )})  # fmt: skip
    with Sandbox(repo, SandboxConfig()) as box:
        denied = box.run([sys.executable, "probe.py"], cwd=str(box.work), env=os.environ, timeout=20, network=False)
        assert denied.network == "denied" and denied.exit_code == 0, denied.output
        assert "blocked" in denied.output
        allowed = box.run([sys.executable, "-c", "pass"], cwd=str(box.work), env=os.environ, timeout=20, network=True)
        assert allowed.network == "allowed" and allowed.exit_code == 0


@pytest.mark.skipif(not (CAN_UNSHARE and shutil.which("ip")), reason="needs namespaces and `ip`")
def test_loopback_still_works_without_network(make_repo):
    repo = make_repo({"loop.py": (
        "import socket\n"
        "server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)\n"
        "socket.create_connection(server.getsockname(), timeout=3).close()\n"
        "print('loopback ok')\n"
    )})  # fmt: skip
    with Sandbox(repo, SandboxConfig()) as box:
        result = box.run([sys.executable, "loop.py"], cwd=str(box.work), env=os.environ, timeout=20, network=False)
        assert result.exit_code == 0 and "loopback ok" in result.output


def test_unenforceable_network_restriction_warns_or_fails(make_repo, monkeypatch):
    monkeypatch.setattr(sandbox_module, "network_namespace_support", lambda: (None, "no namespaces here"))
    repo = make_repo({"README.md": ""})
    with Sandbox(repo, SandboxConfig()) as box:
        result = box.run([sys.executable, "-c", "pass"], cwd=str(box.work), env=os.environ, timeout=20, network=False)
        assert result.network == "unenforced" and result.exit_code == 0
        assert box.describe()["warnings"] == ["network restriction could not be enforced (no namespaces here)"]
    with Sandbox(repo, SandboxConfig(strict=True)) as strict:
        with pytest.raises(IsolationError, match="no namespaces here"):
            strict.run([sys.executable, "-c", "pass"], cwd=str(strict.work), env=os.environ, timeout=20, network=False)


def venv_available() -> bool:
    try:
        import ensurepip  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not venv_available(), reason="venv/ensurepip is unavailable")
def test_ensure_venv_builds_a_pristine_environment(make_repo):
    repo = make_repo({"README.md": ""})
    with Sandbox(repo, SandboxConfig()) as box:
        venv = box.ensure_venv()
        assert box.ensure_venv() == venv  # built once
        probe = subprocess.run(
            [str(venv / "bin" / "python"), "-c", "import sys; print(sys.prefix)"],
            capture_output=True, text=True, check=True,
        )  # fmt: skip
        assert probe.stdout.strip() == str(venv)
