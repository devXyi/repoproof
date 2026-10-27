"""The machine-readable JSON report."""

from __future__ import annotations

import json

from ..models import Report


def render_json(report: Report) -> str:
    """Serialise ``report`` (schema version 1, see docs/reports.md)."""
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
