# Security policy

RepoProof runs commands taken from documentation, so security reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private vulnerability reporting: open the
repository's **Security** tab and choose **Report a vulnerability**. Include what you found,
how to reproduce it (a minimal README or configuration is ideal) and what you expect the impact
to be. You will get an acknowledgement and updates as the report is investigated, and credit in
the release notes if you want it.

## What counts

Examples of things we want to hear about:

- a documented command that runs although it is not on the allowlist or is on the blocklist
  (`sudo`, system package managers, ...), or that reaches outside the temporary workspace;
- shell syntax that is parsed differently from how it is executed;
- secrets reaching the terminal, a report or a pull request comment;
- the GitHub Action exposing a token or running attacker-controlled input as code.

What RepoProof does and does not promise is written down in the
[security model](docs/security-model.md). In particular, an approved command such as
`pip install` or `pytest` runs the repository's own code by design, so that alone is not a
vulnerability.

## Supported versions

Security fixes are made on the latest release and, while the project is in alpha, on `main`.
