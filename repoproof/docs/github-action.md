# GitHub Action

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

The action installs RepoProof into a private virtual environment from its own source, so the
version you get is the version of the tag you pin, and your job's `PATH` and Python are left
alone. It runs on Linux and macOS runners that have Python 3.10 or newer with the `venv`
module (all GitHub-hosted ones do).

`@v0` follows the 0.x releases. Pin an exact tag such as `@v0.1.0`, or a full commit SHA, when
you want reproducible builds. The floating tag becomes `@v1` with the 1.0 release.

## What you get on a pull request

- **Annotations** on the documentation lines that broke, in the Files changed tab where the
  line is part of the diff.
- A **job summary** with the full report.
- One **sticky comment** with the failing command, its output and a hint. It is updated on
  every push and flips to "the documented setup works" once the problem is fixed.
- A failing check when anything at or above `fail-on` was found.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `path` | `.` | Directory to verify, relative to the workspace (useful in monorepos) |
| `config` | | Path to a `repoproof.toml`; by default the one inside `path` |
| `only` | | Comma-separated checks to run: `commands`, `links`, `env` |
| `fail-on` | `error` | `error`, `warning`, `note` or `never` |
| `network` | | `deny`, `install` or `allow`; empty uses the configuration file |
| `comment` | `on-failure` | Pull request comment mode: `on-failure`, `always` or `never` |
| `github-token` | `${{ github.token }}` | Token for the comment |
| `json-file` | | Also write the JSON report here |
| `sarif-file` | | Also write the SARIF report here |
| `python` | `python3` | Interpreter used to run RepoProof (3.10 or newer) |
| `extra-args` | | Extra arguments for `repoproof check`, split on whitespace |

## Recipes

**Verify two documents and keep going after failures**

```yaml
- uses: devXyi/repoproof@v0
  with:
    extra-args: --docs README.md --docs CONTRIBUTING.md --keep-going
```

**A monorepo package with its own configuration**

```yaml
- uses: devXyi/repoproof@v0
  with:
    path: packages/api
    config: packages/api/repoproof.toml
```

**Fast checks only (no commands are executed)**

```yaml
- uses: devXyi/repoproof@v0
  with:
    only: links,env
```

**Upload findings to code scanning**

```yaml
permissions:
  contents: read
  security-events: write

steps:
  - uses: actions/checkout@v7
  - uses: devXyi/repoproof@v0
    with:
      sarif-file: repoproof.sarif
      fail-on: never
  - uses: github/codeql-action/upload-sarif@v4
    if: always()
    with:
      sarif_file: repoproof.sarif
      category: repoproof
```

## Pull requests from forks

GitHub gives workflows triggered by a fork's pull request a read-only token, so the comment
cannot be posted. RepoProof reports that as a warning and carries on; the annotations, the
job summary and the check status still work. Do not "fix" this by switching to
`pull_request_target`: it runs with a write token and secrets, which is exactly what you do not
want next to code from a fork. See the [security model](security-model.md#using-it-safely-on-github).

## Network isolation on hosted runners

Steps that do not need the network are run without it where the runner allows user
namespaces. Hosted runners may not (some images restrict them), in which case RepoProof warns
and the job summary says so; the runner itself is still an ephemeral VM. If you want the job
to fail instead, add `extra-args: --strict-isolation`. Check `isolation.network_enforceable` in
the JSON report to see what your runners support.
