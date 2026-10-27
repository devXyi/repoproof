# RepoProof

**Prove that your repository's setup instructions actually work.**

[![CI](https://github.com/devXyi/repoproof/actions/workflows/ci.yml/badge.svg)](https://github.com/devXyi/repoproof/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

RepoProof is a command-line tool and GitHub Action that follows your README the way a new
contributor would, in a clean, throw-away workspace, and tells you the first command that
breaks together with the line of documentation to fix. It also checks that your documentation
links resolve and that the environment variables your code reads are actually documented.

It is deterministic, written in Python with no third-party runtime dependencies on Python 3.11
and newer, and designed to run on every pull request.

```text
RepoProof 0.1.0 · broken-project

Commands · README.md
  ✔ README.md:9   python3 -m venv .venv  3.0s
  ✖ README.md:10  source venv/bin/activate  failed
                  `source venv/bin/activate` failed: source: no such file: venv/bin/activate
                  Did you mean `.venv/bin/activate`?
  ✔ README.md:11  cp .env.example .env
  ✖ README.md:17  python3 app.py  exit 1
                  `python3 app.py` exited with status 1
                  │ Traceback (most recent call last):
                  │   File "app.py", line 3, in <module>
                  │     database_url = os.environ["DATABASE_URL"]
                  │                    ~~~~~~~~~~^^^^^^^^^^^^^^^^
                  │   File "<frozen os>", line 685, in __getitem__
                  │ KeyError: 'DATABASE_URL'

Findings
  ✖ README.md:20  The link target 'docs/setup.md' does not exist [RP201]
  ▲ README.md:21  The anchor '#installation' was not found in README.md [RP202]
      Anchors come from headings; renaming a heading breaks links to it.
  ▲ app.py:4  `API_TOKEN` is read at app.py:4 but is not documented in README.md or .env.example. [RP301]
  • app.py:5  `DEBUG` is read at app.py:5 but is not documented in README.md or .env.example. [RP301]
  • .env.example:2  `OLD_FLAG` is listed in .env.example but is never read by the code or deployment config. [RP303]

3 errors, 2 warnings, 2 notes  · commands: 4 run, 2 passed, 2 failed, 0 skipped · 6.6s
```

<sub>Real output from the bundled [broken-project demo](examples/broken-project).</sub>

## Quickstart

```bash
pipx install repoproof
repoproof check .
```

RepoProof needs Python 3.10 or newer on Linux or macOS (on Windows, use WSL). Run it from the
root of any repository that has a README with shell commands in it. The five-minute walkthrough
in [docs/quickstart.md](docs/quickstart.md) covers previewing what would run, approving more
commands and adding it to CI.

> **Pre-release.** Until the first version is published to PyPI, install from source with
> `pip install git+https://github.com/devXyi/repoproof`.

## What it checks

| Check | What it verifies | Rules |
| --- | --- | --- |
| **Commands** | Approved commands from your README run successfully, in order, in a clean copy of the repository | RP101 to RP105 |
| **Links** | Relative links, images and heading anchors resolve (case-sensitively, like GitHub) | RP201, RP202 |
| **Environment** | Variables the code reads are documented in the README or `.env.example` and present in deployment config | RP301 to RP304 |

Every finding has a stable rule ID, an exact file and line, and, for commands, the failing
command and the end of its output. See the [rule reference](docs/rules.md) and
[docs/checks.md](docs/checks.md) for how each check decides.

## How it works

1. The repository is copied into a temporary workspace: what a fresh clone plus your
   uncommitted files would contain.
2. Commands are extracted from fenced shell blocks in the README (`bash`, `sh`, `console`
   blocks with `$` prompts, and so on) with their exact line numbers.
3. Each command is parsed by a small, strict parser and checked against an allowlist. Anything
   that is not approved, or is dangerous (`sudo`, system package managers, pipes into a
   shell), is reported as skipped and never runs.
4. Approved commands run in order in a scrubbed environment: temporary `HOME`, no inherited
   secrets, a pristine virtualenv for Python, timeouts, and no network for steps that do not
   need it where the operating system allows.
5. You get the first command that breaks, its output and the documentation line to fix, plus
   the link and configuration findings. Exit status, JSON, SARIF and Markdown reports are
   built for CI.

Like a real newcomer, RepoProof stops at the first failing step unless you ask it to keep
going. Commands that need system packages, services or interactive input are skipped, and
when a skipped command is the likely cause of a later failure the report says so.

## Use it in GitHub Actions

```yaml
name: Repository verification

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  pull-requests: write # only needed for the pull request comment

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: devXyi/repoproof@v0
```

On a pull request the action annotates the exact documentation lines, writes a job summary
and keeps a single sticky comment up to date with the failing command and its output. It can
also write JSON and SARIF reports for code scanning. See [docs/github-action.md](docs/github-action.md).

## Configure it

Everything is optional. `repoproof init` writes a commented starter file.

```toml
# repoproof.toml
[repoproof]
docs = ["README.md", "CONTRIBUTING.md"] # each runs as its own journey in a fresh workspace

[commands]
allow = ["mytool *"]         # approve commands beyond the built-in allowlist
skip = ["docker compose *"]  # documented commands that must never run

[[commands.rewrite]]         # verify the checkout instead of the published package
match = "pip install mypackage"
run = "pip install ."
```

Add `<!-- repoproof:skip -->` on the line before a code block to leave it out. The full
reference is in [docs/configuration.md](docs/configuration.md).

## Security model

RepoProof never blindly executes what a README says.

- **No shell.** Commands are parsed into an argument list and executed directly. Pipes,
  command substitution, heredocs, subshells and similar syntax are reported as unsupported,
  never guessed at.
- **Explicit approval.** Only commands on the built-in allowlist, in your `allow` list or
  shipped by the project itself run. A line runs only if every command on it is approved.
- **Hard blocks.** `sudo`, system package managers and similar commands never run, and no
  configuration can allow them.
- **Isolation.** Temporary workspace and `HOME`, no inherited secrets, process-group timeouts,
  bounded output, secret redaction, and a network namespace for steps that should not use the
  network (Linux with user namespaces; elsewhere RepoProof warns, or fails with
  `--strict-isolation`).

An approved `pip install` or `pytest` still executes the repository's own code, so RepoProof is
a safety rail for well-meaning documentation, not a sandbox for hostile code. Verify untrusted
repositories in a disposable VM or container, which is what a GitHub-hosted runner is. Read
the [security model](docs/security-model.md) for what is and is not guaranteed.

## Supported ecosystems

| Ecosystem | Commands recognised | Clean environment | Environment-variable scan |
| --- | --- | --- | --- |
| Python | `pip`, `python -m venv`, `uv`, `poetry`, `pdm`, `tox`, `nox`, `pytest`, `python script.py` | fresh virtualenv per journey | yes |
| Node.js | `npm`, `yarn`, `pnpm` install, test and build | generic sandbox | yes |
| Go | `go mod download`, `build`, `test`, `vet`, `run` | generic sandbox | yes |
| Rust | `cargo fetch`, `build`, `check`, `test`, `run` | generic sandbox | yes |

Python is first-class today. Node.js, Go and Rust commands are recognised and run in the
generic sandbox; ecosystem-specific clean environments are on the roadmap.

## Status and roadmap

RepoProof is alpha software. It is exercised by its own test suite, its own README and the
bundled demo project. Running it against a set of well-known open-source projects and
publishing what it finds is the next milestone.

- [x] Reliable CLI: approved commands, test execution, internal link checks
- [x] Configuration intelligence: environment variables across code, docs, `.env.example` and deployment files
- [x] GitHub Action: annotations, job summary, sticky pull request comment, JSON and SARIF
- [ ] Language adapters with ecosystem-specific clean environments (Node.js, Go, Rust)
- [ ] A container backend, so network isolation is enforceable on any CI runner
- [ ] Caching and reusable repository profiles
- [ ] Opt-in external link checking
- [ ] AI-generated failure explanations, once the deterministic core has earned them

## Development

```bash
git clone https://github.com/devXyi/repoproof && cd repoproof
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

This repository verifies its own README: the [self-check workflow](.github/workflows/repoproof.yml)
runs RepoProof on every pull request, using [repoproof.toml](repoproof.toml) to test the
checkout instead of a published release.

## Contributing

Contributions are welcome and beginners are too. [CONTRIBUTING.md](CONTRIBUTING.md) explains the
code layout, how to add a rule or an allowlisted command, and where to find a good first issue.
Please also read the [code of conduct](CODE_OF_CONDUCT.md). Report security problems privately
as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
