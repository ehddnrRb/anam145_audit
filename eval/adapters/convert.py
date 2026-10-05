"""Findings -> the scan-results shape scoring_algo.core.storage.read_scan_results reads."""
from adapters.base import Findings


def to_scorer(findings: Findings, project_id: str) -> dict:
    out = []
    for f in findings.vulnerabilities:
        if not f.description.strip():
            raise ValueError(f"finding {f.id!r} has an empty description")
        # severity passes through; the scorer folds critical->High, info->Info
        out.append({"title": f.title, "description": f.description,
                    "severity": f.severity, "file": f.file})
    return {"project": project_id, "findings": out}
