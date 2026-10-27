"""Configuration: the small ``repoproof.toml`` format.

Every key is optional. Unknown keys are errors, so a typo never silently disables a check.
"""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .shellparse import ShellError, parse

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - only used on Python 3.10
    import tomli as tomllib

CONFIG_FILENAME = "repoproof.toml"
FAIL_ON_CHOICES = ("error", "warning", "note", "never")
NETWORK_CHOICES = ("deny", "install", "allow")
PHASES = ("setup", "install", "build", "test", "run", "info")
CHECKS = ("commands", "links", "env")


class ConfigError(ValueError):
    """Raised for an invalid configuration file."""


@dataclass(frozen=True)
class AllowRule:
    """A command pattern that may be executed. ``*`` is the only wildcard."""

    pattern: str
    phase: str = "run"
    network: bool | None = None
    timeout: int | None = None


@dataclass(frozen=True)
class RewriteRule:
    """Replace a documented command with another before it is vetted and run."""

    match: str
    run: str


@dataclass
class SandboxConfig:
    python: str = "auto"
    network: str = "install"
    timeout: int = 300
    timeouts: dict[str, int] = field(
        default_factory=lambda: {"install": 900, "build": 600, "test": 600}
    )
    total_timeout: int = 1800
    keep_going: bool = False
    strict: bool = False
    pass_env: list[str] = field(default_factory=list)
    inherit_proxy: bool = True
    max_output_bytes: int = 65536


@dataclass
class LinksConfig:
    enabled: bool = True
    ignore: list[str] = field(default_factory=list)


@dataclass
class EnvConfig:
    enabled: bool = True
    ignore: list[str] = field(default_factory=list)
    include_tests: bool = False
    exclude: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    deployment: list[str] = field(default_factory=list)


@dataclass
class Config:
    docs: list[str] = field(default_factory=list)
    fail_on: str = "error"
    allow: list[AllowRule] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    rewrite: list[RewriteRule] = field(default_factory=list)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    links: LinksConfig = field(default_factory=LinksConfig)
    env: EnvConfig = field(default_factory=EnvConfig)
    path: Path | None = None


def read_toml(path: Path) -> dict[str, Any]:
    """Best-effort TOML read: an unreadable or invalid file yields an empty dict."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}


def find_config(root: Path) -> Path | None:
    candidate = root / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load_config(root: Path, explicit: Path | None = None) -> Config:
    """Load ``repoproof.toml`` from ``root`` (or ``explicit``). Missing means defaults."""
    path = explicit if explicit is not None else find_config(root)
    if path is None:
        return Config()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name}: invalid TOML: {exc}") from exc
    try:
        config = parse_config(data)
    except ConfigError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    config.path = path
    return config


def parse_config(data: dict[str, Any]) -> Config:
    """Validate a decoded TOML document and build a :class:`Config`."""
    _check_keys(data, {"version", "repoproof", "sandbox", "commands", "links", "env"}, "top level")
    if "version" in data and data["version"] != 1:
        raise ConfigError(f"unsupported config version {data['version']!r} (expected 1)")

    config = Config()

    top = _table(data, "repoproof")
    _check_keys(top, {"docs", "fail_on"}, "[repoproof]")
    config.docs = _str_list(top, "docs", "[repoproof]")
    config.fail_on = _choice(top, "fail_on", "[repoproof]", FAIL_ON_CHOICES, config.fail_on)

    box = _table(data, "sandbox")
    allowed = {
        "python", "network", "timeout", "timeouts", "total_timeout", "keep_going",
        "strict", "pass_env", "inherit_proxy", "max_output_bytes",
    }  # fmt: skip
    _check_keys(box, allowed, "[sandbox]")
    sandbox = config.sandbox
    sandbox.python = _str(box, "python", "[sandbox]", sandbox.python)
    sandbox.network = _choice(box, "network", "[sandbox]", NETWORK_CHOICES, sandbox.network)
    sandbox.timeout = _int(box, "timeout", "[sandbox]", sandbox.timeout, minimum=1)
    sandbox.total_timeout = _int(box, "total_timeout", "[sandbox]", sandbox.total_timeout, 1)
    sandbox.keep_going = _bool(box, "keep_going", "[sandbox]", sandbox.keep_going)
    sandbox.strict = _bool(box, "strict", "[sandbox]", sandbox.strict)
    sandbox.inherit_proxy = _bool(box, "inherit_proxy", "[sandbox]", sandbox.inherit_proxy)
    sandbox.pass_env = _str_list(box, "pass_env", "[sandbox]")
    sandbox.max_output_bytes = _int(
        box, "max_output_bytes", "[sandbox]", sandbox.max_output_bytes, minimum=1024
    )
    timeouts = box.get("timeouts", {})
    if not isinstance(timeouts, dict):
        raise ConfigError("[sandbox.timeouts] must be a table")
    _check_keys(timeouts, set(PHASES), "[sandbox.timeouts]")
    for phase in timeouts:
        sandbox.timeouts[phase] = _int(timeouts, phase, "[sandbox.timeouts]", 0, minimum=1)

    commands = _table(data, "commands")
    _check_keys(commands, {"allow", "skip", "rewrite"}, "[commands]")
    config.skip = _str_list(commands, "skip", "[commands]")
    config.allow = [_allow_rule(item) for item in _list(commands, "allow", "[commands]")]
    config.rewrite = [_rewrite_rule(item) for item in _list(commands, "rewrite", "[commands]")]

    links = _table(data, "links")
    _check_keys(links, {"enabled", "ignore"}, "[links]")
    config.links.enabled = _bool(links, "enabled", "[links]", True)
    config.links.ignore = _str_list(links, "ignore", "[links]")

    env = _table(data, "env")
    allowed_env = {"enabled", "ignore", "include_tests", "exclude", "examples", "deployment"}
    _check_keys(env, allowed_env, "[env]")
    config.env.enabled = _bool(env, "enabled", "[env]", True)
    config.env.ignore = _str_list(env, "ignore", "[env]")
    config.env.include_tests = _bool(env, "include_tests", "[env]", False)
    config.env.exclude = _str_list(env, "exclude", "[env]")
    config.env.examples = _str_list(env, "examples", "[env]")
    config.env.deployment = _str_list(env, "deployment", "[env]")
    return config


def _allow_rule(item: Any) -> AllowRule:
    if isinstance(item, str):
        return AllowRule(pattern=_non_empty(item, "[commands] allow"))
    if not isinstance(item, dict):
        raise ConfigError("[commands] allow entries must be strings or tables")
    _check_keys(item, {"pattern", "phase", "network", "timeout"}, "[commands] allow entry")
    if "pattern" not in item:
        raise ConfigError("[commands] allow entry is missing 'pattern'")
    network = item.get("network")
    if network is not None and not isinstance(network, bool):
        raise ConfigError("[commands] allow entry: 'network' must be true or false")
    return AllowRule(
        pattern=_non_empty(item["pattern"], "[commands] allow entry 'pattern'"),
        phase=_choice(item, "phase", "[commands] allow entry", PHASES, "run"),
        network=network,
        timeout=_int(item, "timeout", "[commands] allow entry", 0, minimum=1) or None,
    )


def _rewrite_rule(item: Any) -> RewriteRule:
    if not isinstance(item, dict):
        raise ConfigError("[[commands.rewrite]] entries must be tables")
    _check_keys(item, {"match", "run"}, "[[commands.rewrite]]")
    match = _non_empty(item.get("match"), "[[commands.rewrite]] 'match'")
    run = _non_empty(item.get("run"), "[[commands.rewrite]] 'run'")
    try:
        parse(run)
    except ShellError as exc:
        raise ConfigError(f"[[commands.rewrite]] 'run' is not a runnable command: {exc}") from exc
    return RewriteRule(match=match, run=run)


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _check_keys(table: dict[str, Any], allowed: set[str], where: str) -> None:
    for key in table:
        if key not in allowed:
            close = difflib.get_close_matches(key, sorted(allowed), n=1)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            raise ConfigError(f"unknown key '{key}' in {where}{hint}")


def _list(table: dict[str, Any], key: str, where: str) -> list[Any]:
    value = table.get(key, [])
    if not isinstance(value, list):
        raise ConfigError(f"{where} {key} must be a list")
    return value


def _str_list(table: dict[str, Any], key: str, where: str) -> list[str]:
    value = _list(table, key, where)
    if not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where} {key} must be a list of strings")
    return list(value)


def _str(table: dict[str, Any], key: str, where: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} {key} must be a non-empty string")
    return value


def _non_empty(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where} must be a non-empty string")
    return value


def _bool(table: dict[str, Any], key: str, where: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where} {key} must be true or false")
    return value


def _int(table: dict[str, Any], key: str, where: str, default: int, minimum: int = 0) -> int:
    if key not in table:
        return default
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} {key} must be an integer >= {minimum}")
    return value


def _choice(
    table: dict[str, Any], key: str, where: str, choices: tuple[str, ...], default: str
) -> str:
    value = table.get(key, default)
    if value not in choices:
        raise ConfigError(f"{where} {key} must be one of: {', '.join(choices)}")
    return str(value)
