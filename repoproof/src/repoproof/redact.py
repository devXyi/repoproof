"""Secret redaction.

Everything RepoProof captures from a command, and every command line it prints, goes through
a :class:`Redactor` before it can reach a terminal, a report or a pull-request comment.

Two layers are applied:

* *exact values*: the values of host environment variables whose names look sensitive
  (``*_TOKEN``, ``*_SECRET``, ``*PASSWORD*`` ...) and of every variable named in
  ``[sandbox] pass_env``;
* *patterns*: well-known credential formats (GitHub, AWS, Slack, Stripe and Google tokens,
  JWTs, private keys, credentials embedded in URLs, ``NAME=value`` secret assignments).

Redaction is a safety net, not a guarantee. The primary defence is that secrets are never
passed to documented commands in the first place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

REDACTED = "***REDACTED***"

_SENSITIVE_NAME = re.compile(
    r"secret|token|passw|credential|api[_-]?key|private[_-]?key|access[_-]?key|auth",
    re.IGNORECASE,
)
_MIN_SECRET_LENGTH = 8

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        REDACTED,
    ),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), REDACTED),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), REDACTED),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), REDACTED),
    (re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), REDACTED),
    (re.compile(r"(?i)\b(bearer|token)(\s+)[A-Za-z0-9._~+/=-]{16,}"), r"\1\2" + REDACTED),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + REDACTED + r"\2"),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)"
            r"[A-Z0-9_]*)(\s*[=:]\s*)(['\"]?)[^\s'\"]{4,}\3"
        ),
        r"\1\2\3" + REDACTED + r"\3",
    ),
)


class Redactor:
    """Replace secrets with ``***REDACTED***``."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        values = {s for s in secrets if len(s) >= _MIN_SECRET_LENGTH}
        self._values = sorted(values, key=len, reverse=True)

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str], extra_names: Iterable[str] = ()
    ) -> Redactor:
        """Collect secret-looking values from an environment mapping."""
        extra = set(extra_names)
        values = [
            value
            for name, value in environ.items()
            if name in extra or _SENSITIVE_NAME.search(name)
        ]
        return cls(values)

    def redact(self, text: str) -> str:
        if not text:
            return text
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text
