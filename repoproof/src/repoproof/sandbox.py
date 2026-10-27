"""The local sandbox: a throw-away copy of the repository and a scrubbed environment.

What this gives you, and what it does not, is spelled out in docs/security-model.md. In short:
documented commands run in a temporary copy of the repository, with a temporary ``HOME``,
no inherited secrets, a timeout, bounded output, their own process group (killed on
timeout) and, where Linux allows it, without network access. It is a safety rail for
well-meaning documentation, not a security boundary against hostile code.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import SandboxConfig

IGNORED_NAMES = frozenset(
    {
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".nox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".DS_Store",
    }
)  # fmt: skip

_PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT",
)  # fmt: skip
_NETNS_SCRIPT = 'command -v ip >/dev/null 2>&1 && ip link set lo up >/dev/null 2>&1; exec "$@"'


class SandboxError(RuntimeError):
    """The clean environment could not be prepared."""


class IsolationError(SandboxError):
    """A requested isolation guarantee cannot be provided on this host."""


@dataclass(frozen=True)
class RedirectSpec:
    """Where a command's stdout/stderr go: ``"capture"``, ``"null"`` or ``("file", path, append)``."""

    stdout: Any = "capture"
    stderr: Any = "capture"


@dataclass
class ProcessResult:
    exit_code: int | None
    timed_out: bool
    output: str
    truncated: bool
    duration: float
    network: str = "allowed"  # allowed | denied | unenforced


def clean_path(path: str, host: Mapping[str, str]) -> str:
    """Drop the host's active virtualenv/conda ``bin`` so the run starts from a clean slate."""
    drop = set()
    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        if host.get(var):
            drop.add(os.path.normpath(os.path.join(host[var], "bin")))
    entries = [e for e in path.split(os.pathsep) if e and os.path.normpath(e) not in drop]
    return os.pathsep.join(entries)


def build_base_env(
    host: Mapping[str, str],
    *,
    home: str,
    tmp: str,
    pass_env: Iterable[str] = (),
    inherit_proxy: bool = True,
) -> dict[str, str]:
    """The environment documented commands start with. Nothing is inherited by accident."""
    env = {
        "PATH": clean_path(host.get("PATH", os.defpath), host),
        "HOME": home,
        "TMPDIR": tmp,
        "TMP": tmp,
        "TEMP": tmp,
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "LANG": host.get("LANG", "C.UTF-8"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "PYTHONUNBUFFERED": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INPUT": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "REPOPROOF_ACTIVE": "1",
    }
    for name in ("LANGUAGE", "TZ"):
        if name in host:
            env[name] = host[name]
    for name, value in host.items():
        if name.startswith("LC_"):
            env[name] = value
    if inherit_proxy:
        for name in _PROXY_VARS:
            if name in host:
                env[name] = host[name]
    for name in pass_env:
        if name in host:
            env[name] = host[name]
    return env


def copy_repo(src: Path, dst: Path, *, tracked_only: bool = False) -> None:
    """Copy the repository into ``dst``.

    Inside a git checkout this copies what a fresh clone plus your uncommitted-but-not-ignored
    files would contain (``tracked_only`` limits it to tracked files, like a pure clone).
    Elsewhere it copies the tree minus caches, virtualenvs and VCS data.
    """
    files = _git_files(src, tracked_only)
    dst.mkdir(parents=True, exist_ok=True)
    if files is None:

        def ignore(_directory: str, names: list[str]) -> list[str]:
            return [n for n in names if n in IGNORED_NAMES or n.endswith(".egg-info")]

        shutil.copytree(src, dst, symlinks=True, ignore=ignore, dirs_exist_ok=True)
        return
    for rel in files:
        source = src / rel
        if not (source.exists() or source.is_symlink()):
            continue  # listed in the index but deleted in the working tree
        if source.is_dir() and not source.is_symlink():
            continue  # submodule
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        else:
            shutil.copy2(source, target)


def _git_files(src: Path, tracked_only: bool) -> list[str] | None:
    if shutil.which("git") is None:
        return None
    command = ["git", "-C", str(src), "ls-files", "-z", "--cached"]
    if not tracked_only:
        command += ["--others", "--exclude-standard"]
    try:
        proc = subprocess.run(command, capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "surrogateescape")
    return [name for name in text.split("\0") if name]


@lru_cache(maxsize=1)
def network_namespace_support() -> tuple[str | None, str]:
    """Return ``(path to unshare, "")`` if network namespaces work, else ``(None, reason)``."""
    if sys.platform != "linux":
        return None, "network namespaces are only available on Linux"
    exe = shutil.which("unshare")
    if exe is None:
        return None, "`unshare` was not found"
    try:
        probe = subprocess.run(
            [exe, "--net", "--map-root-user", "--", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if probe.returncode == 0:
        return exe, ""
    detail = probe.stderr.decode("utf-8", "replace").strip()
    return None, detail or f"unshare exited with status {probe.returncode}"


class _Tail:
    """Keeps only the last ``limit`` bytes of a stream."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.data = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self.data += chunk
        if len(self.data) > self.limit * 2:
            del self.data[: len(self.data) - self.limit]

    def value(self) -> tuple[bytes, bool]:
        tail = bytes(self.data[-self.limit :])
        return tail, self.total > len(tail)


def _pump(fd: int, tail: _Tail) -> None:
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            return
        tail.feed(chunk)


def _kill_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except OSError:
        pass


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    _kill_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc, signal.SIGKILL)
    proc.wait()


def _open_target(target: Any, opened: list[Any]) -> Any:
    if target == "capture":
        return subprocess.PIPE
    if target == "null":
        return subprocess.DEVNULL
    _kind, path, append = target
    handle = open(path, "ab" if append else "wb")  # noqa: SIM115 - closed by the caller
    opened.append(handle)
    return handle


def run_process(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
    max_bytes: int,
    redirects: RedirectSpec | None = None,
) -> ProcessResult:
    """Run ``argv`` (no shell) and return its exit status and the tail of its output."""
    spec = redirects or RedirectSpec()
    opened: list[Any] = []
    started = time.monotonic()
    tail = _Tail(max_bytes)
    try:
        stdout = _open_target(spec.stdout, opened)
        stderr = subprocess.STDOUT if spec.stdout == spec.stderr else _open_target(
            spec.stderr, opened
        )
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        for handle in opened:
            handle.close()
        return ProcessResult(127, False, f"{exc}\n", False, time.monotonic() - started)

    reader: threading.Thread | None = None
    stream = None
    if isinstance(stdout, int) and stdout == subprocess.PIPE:
        stream = proc.stdout
    elif isinstance(stderr, int) and stderr == subprocess.PIPE:
        stream = proc.stderr
    if stream is not None:
        reader = threading.Thread(target=_pump, args=(stream.fileno(), tail), daemon=True)
        reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc)
    finally:
        for handle in opened:
            handle.close()
    if reader is not None:
        reader.join(timeout=5 if timed_out else 2)
        if reader.is_alive():  # something that outlived the command still holds the pipe
            _kill_group(proc, signal.SIGKILL)
            reader.join(timeout=2)

    data, truncated = tail.value()
    text = data.decode("utf-8", "replace")
    if truncated:
        text = "[... earlier output truncated ...]\n" + text
    return ProcessResult(proc.returncode, timed_out, text, truncated, time.monotonic() - started)


@dataclass
class Sandbox:
    """A temporary workspace plus the environment documented commands run in."""

    repo_root: Path
    cfg: SandboxConfig
    tracked_only: bool = False
    keep: bool = False
    host_env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    root: Path = field(init=False)
    work: Path = field(init=False)
    home: Path = field(init=False)
    tmp: Path = field(init=False)
    base_env: dict[str, str] = field(init=False)
    warnings: list[str] = field(default_factory=list)
    _venv: Path | None = None

    def __enter__(self) -> Sandbox:
        self.prepare()
        return self

    def __exit__(self, *_exc: object) -> None:
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)

    def prepare(self) -> None:
        try:
            self.root = Path(os.path.realpath(tempfile.mkdtemp(prefix="repoproof-")))
            self.work = self.root / "work"
            self.home = self.root / "home"
            self.tmp = self.root / "tmp"
            self.home.mkdir()
            self.tmp.mkdir()
            copy_repo(self.repo_root, self.work, tracked_only=self.tracked_only)
        except (OSError, shutil.Error) as exc:
            if getattr(self, "root", None) is not None:
                shutil.rmtree(self.root, ignore_errors=True)
            raise SandboxError(f"cannot create the workspace: {exc}") from exc
        self.base_env = build_base_env(
            self.host_env,
            home=str(self.home),
            tmp=str(self.tmp),
            pass_env=self.cfg.pass_env,
            inherit_proxy=self.cfg.inherit_proxy,
        )

    @property
    def python_enabled(self) -> bool:
        return self.cfg.python != "none"

    def ensure_venv(self) -> Path:
        """Create (once) the pristine virtual environment commands run in."""
        if self._venv is not None:
            return self._venv
        interpreter = sys.executable if self.cfg.python in ("auto", "") else self.cfg.python
        venv = self.root / "venv"
        try:
            proc = subprocess.run(
                [interpreter, "-m", "venv", str(venv)],
                env=self.base_env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SandboxError(f"could not run `{interpreter} -m venv`: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise SandboxError(f"`{interpreter} -m venv` failed: {detail}")
        self._venv = venv
        return venv

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        timeout: float,
        network: bool,
        redirects: RedirectSpec | None = None,
    ) -> ProcessResult:
        """Run one command, without network access unless ``network`` is true."""
        final = list(argv)
        state = "allowed"
        if not network:
            unshare, reason = network_namespace_support()
            if unshare is not None:
                final = [unshare, "--net", "--map-root-user", "--", "sh", "-c", _NETNS_SCRIPT, "sh", *argv]
                state = "denied"
            elif self.cfg.strict:
                raise IsolationError(f"cannot enforce the network restriction: {reason}")
            else:
                state = "unenforced"
                note = f"network restriction could not be enforced ({reason})"
                if note not in self.warnings:
                    self.warnings.append(note)
        result = run_process(
            final,
            cwd=cwd,
            env=env,
            timeout=timeout,
            max_bytes=self.cfg.max_output_bytes,
            redirects=redirects,
        )
        result.network = state
        return result

    def describe(self) -> dict[str, Any]:
        """Isolation facts for the report."""
        return {
            "backend": "local",
            "network_policy": self.cfg.network,
            "network_enforceable": network_namespace_support()[0] is not None,
            "python_env": self.cfg.python,
            "strict": self.cfg.strict,
            "warnings": list(self.warnings),
        }
