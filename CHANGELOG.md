# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - Unreleased

First public version.

### Added

- `repoproof check`: runs the approved shell commands from a repository's documentation in a
  temporary workspace with a scrubbed environment, timeouts, bounded output, secret redaction
  and (on Linux) no network for steps that do not need it.
- A strict command parser (no shell is ever invoked), a built-in allowlist for Python, Node.js,
  Go and Rust workflows, a hard blocklist, workspace-confined file commands and per-project
  configuration of what may run.
- Emulation of `cd`, `export`, `source` (virtualenv activation and `.env` files) and
  `git clone`, so documented journeys behave like a terminal session.
- Link check: relative links, images and heading anchors, case-sensitive like GitHub.
- Environment-variable check across Python, JavaScript/TypeScript, Go and Rust code,
  documentation, `.env.example` files and deployment configuration.
- Reports: text, JSON, SARIF 2.1.0 and Markdown; GitHub annotations, job summary and a sticky
  pull request comment.
- `uses: devXyi/repoproof` composite GitHub Action, `repoproof init` and `repoproof rules`.
