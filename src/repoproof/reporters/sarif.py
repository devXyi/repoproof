"""SARIF 2.1.0 output, for GitHub code scanning and other SARIF consumers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..models import RULES, Finding, Report, RuleInfo

SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
INFORMATION_URI = "https://github.com/devXyi/repoproof"


def render_sarif(report: Report) -> str:
    """Serialise ``report`` as a SARIF log with one run."""
    rules = list(RULES.values())
    index = {rule.id: position for position, rule in enumerate(rules)}
    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "RepoProof",
                "version": report.version,
                "informationUri": INFORMATION_URI,
                "rules": [_rule(rule) for rule in rules],
            }
        },
        "results": [_result(f, index[f.rule_id]) for f in report.findings],
        "invocations": [{"executionSuccessful": True}],
    }
    log = {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": [run]}
    return json.dumps(log, indent=2, ensure_ascii=False) + "\n"


def fingerprint(finding: Finding) -> str:
    """A stable identity so code scanning can track a finding across commits."""
    basis = "|".join([finding.rule_id, finding.file or "", finding.command or finding.message])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _rule(rule: RuleInfo) -> dict[str, Any]:
    return {
        "id": rule.id,
        "name": "".join(part.capitalize() for part in rule.name.split("-")),
        "shortDescription": {"text": rule.summary},
        "fullDescription": {"text": f"{rule.summary} {rule.help}"},
        "help": {"text": rule.help},
        "helpUri": f"{INFORMATION_URI}/blob/main/docs/rules.md#{rule.id.lower()}",
        "defaultConfiguration": {"level": rule.severity.value},
        "properties": {"tags": ["documentation", "onboarding"]},
    }


def _result(finding: Finding, rule_index: int) -> dict[str, Any]:
    text = finding.message + (f" {finding.hint}" if finding.hint else "")
    result: dict[str, Any] = {
        "ruleId": finding.rule_id,
        "ruleIndex": rule_index,
        "level": finding.level.value,
        "message": {"text": text},
        "partialFingerprints": {"repoproof/v1": fingerprint(finding)},
    }
    if finding.file:
        physical: dict[str, Any] = {
            "artifactLocation": {"uri": finding.file, "uriBaseId": "%SRCROOT%"}
        }
        if finding.line:
            physical["region"] = {"startLine": finding.line}
        result["locations"] = [{"physicalLocation": physical}]
    return result
