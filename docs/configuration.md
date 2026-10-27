# Configuration reference

RepoProof reads `repoproof.toml` from the repository root (or the file given with
`--config`). Every key is optional and unknown keys are errors, so a typo never silently
disables a check. `repoproof init` writes a commented starter file. Command-line flags override
the file.

```toml
version = 1        # optional; the only supported value is 1
```

## `[repoproof]`

| Key | Default | Meaning |
| --- | --- | --- |
| `docs` | `[]` | Documentation files to verify. Empty means `README.md`. Each file runs as its own journey in a fresh workspace, so documents never interfere with each other. |
| `fail_on` | `"error"` | Severity that makes the exit status `1`: `"error"`, `"warning"`, `"note"` or `"never"`. |

## `[sandbox]`

| Key | Default | Meaning |
| --- | --- | --- |
| `python` | `"auto"` | `"auto"` builds a pristine virtualenv the first time a Python tool runs, `"none"` turns that off, anything else names the interpreter to build it with (for example `"python3.12"`). |
| `network` | `"install"` | `"deny"`: no network for any command. `"install"`: network only for steps that need it (package installs and builds that fetch dependencies, or commands you mark with `network = true`). `"allow"`: no restriction. |
| `timeout` | `300` | Seconds a command may run when its phase has no entry in `timeouts`. |
| `timeouts` | see below | Per-phase seconds. Phases: `setup`, `install`, `build`, `test`, `run`, `info`. Defaults: `install = 900`, `build = 600`, `test = 600`. |
| `total_timeout` | `1800` | Seconds for the whole run. Once spent, the current step times out. |
| `keep_going` | `false` | Continue after a failing step instead of stopping where a newcomer would be stuck. |
| `strict` | `false` | Fail with `RP105` instead of running a step without an isolation guarantee that was requested but cannot be enforced. |
| `pass_env` | `[]` | Host environment variables to pass into the sandbox. Nothing else is inherited, apart from locale variables and, unless `inherit_proxy` is `false`, proxy and certificate settings. |
| `inherit_proxy` | `true` | Pass `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE` and friends through. |
| `max_output_bytes` | `65536` | Keep this much of the end of each command's output (minimum 1024). |

## `[commands]`

| Key | Meaning |
| --- | --- |
| `allow` | Extra commands that may run. Strings, or tables with `pattern`, `phase`, `network` (true or false) and `timeout`. |
| `skip` | Commands that must never run. They are reported as skipped. |
| `rewrite` | An array of `[[commands.rewrite]]` tables with `match` and `run`; see below. |

**Patterns.** `*` is the only wildcard and matches anything, including spaces; everything else
is literal, so `pip install -e .[dev]` means exactly that. A pattern is matched against the
command's words joined by single spaces, after quotes are removed and variables expanded.
Version suffixes are normalised, so `python3.12` and `pip3` also match patterns written
with `python` and `pip`. `"mytool *"` needs at least one argument; use `"mytool"` for the bare
command.

```toml
[commands]
allow = [
  "mytool *",
  { pattern = "make fetch-data", phase = "install", network = true, timeout = 600 },
]
skip = ["docker compose *"]
```

`skip` wins over `allow`. Commands on the built-in hard blocklist (privilege escalation,
system package managers and a few others, see the [security model](security-model.md))
cannot be allowed by any configuration.

**Rewrites.** A rewrite replaces a documented command before it is vetted, which is how you
test the checkout instead of a published release. `match` is compared with the command's
words as written in the documentation (variables appear as `$NAME`); `run` may contain
several commands joined by `&&`. The replacement goes through the same policy as everything
else.

```toml
[[commands.rewrite]]
match = "pipx install repoproof"
run = "pip install ."
```

## `[links]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Run the link check. |
| `ignore` | `[]` | Link targets to skip. Patterns use the same `*` wildcard and are matched against both the written target and the repository path it resolves to. |

## `[env]`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Run the environment-variable check. |
| `ignore` | `[]` | Variable names to leave out, in addition to the built-in list of ubiquitous ones (`PATH`, `HOME`, `CI`, `GITHUB_*`, `PYTHON*`, ...). `*` wildcards work. |
| `include_tests` | `false` | Also scan test code (`tests/`, `test_*.py`, `*.spec.*`, ...). |
| `exclude` | `[]` | Source paths to skip, for example `["examples/*"]`. |
| `examples` | `[]` | Extra `.env.example`-style files, besides the ones found automatically. |
| `deployment` | `[]` | Extra deployment files, besides the compose files, Dockerfiles and Kubernetes manifests found automatically. |

## Command-line overrides

| Flag | Overrides |
| --- | --- |
| `--docs FILE` (repeatable) | `[repoproof] docs` |
| `--fail-on LEVEL` | `[repoproof] fail_on` |
| `--network MODE` | `[sandbox] network` |
| `--timeout SECONDS` | `[sandbox] timeout` |
| `--keep-going` | `[sandbox] keep_going = true` |
| `--strict-isolation` | `[sandbox] strict = true` |
