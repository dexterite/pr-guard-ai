"""Format check results and sanitize any accidentally leaked secrets."""

import json
import re
import os
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_ICONS = {
    "critical": "\U0001f534",   # 🔴
    "high":     "\U0001f7e0",   # 🟠
    "medium":   "\U0001f7e1",   # 🟡
    "low":      "\U0001f535",   # 🔵
    "info":     "\u26aa",       # ⚪
}

SEVERITY_ORDER = ["critical", "high", "medium", "low", "info"]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def format_report(results: list[dict], config: dict) -> str:
    """Format results into the requested output format."""
    fmt = config.get("output_format", "markdown")
    if fmt == "json":
        return _format_json(results, config)
    if fmt == "sarif":
        return _format_sarif(results, config)
    return _format_markdown(results, config)


def sanitize_output(report: str, config: dict) -> str:
    """Strip any sensitive material that may have leaked into the report."""
    sanitized = report

    for pattern in _SENSITIVE_RE:
        sanitized = pattern.sub("[REDACTED]", sanitized)

    # Redact the actual API key
    api_key = config.get("api_key", "")
    if api_key and len(api_key) > 8:
        sanitized = sanitized.replace(api_key, "[REDACTED]")

    return sanitized


# ---------------------------------------------------------------------------
# Markdown formatter
# ---------------------------------------------------------------------------


def _format_markdown(results: list[dict], config: dict) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


    lines.append("# \U0001f6e1\ufe0f PR Guard AI Report\n")
    lines.append(f"**Date:** {now}  ")
    lines.append(f"**Model:** `{config.get('model', 'n/a')}`  ")
    lines.append(f"**Checks:** {', '.join(config.get('enabled_checks', []))}\n")

    # -- Summary table -----------------------------------------------------
    total = sum(len(r["findings"]) for r in results)

    lines.append("## Summary\n")
    lines.append("| Check | Files | Findings |")
    lines.append("|-------|------:|:--------:|")
    for r in results:
        lines.append(f"| {r['check']} | {r['files_analyzed']} | {len(r['findings'])} |")
    lines.append(f"| **Total** | | **{total}** |\n")

    # -- Severity breakdown ------------------------------------------------
    sev_counts: dict[str, int] = {}
    for r in results:
        for f in r["findings"]:
            s = f.get("severity", "info")
            sev_counts[s] = sev_counts.get(s, 0) + 1

    if sev_counts:
        lines.append("### Severity Breakdown\n")
        for s in SEVERITY_ORDER:
            c = sev_counts.get(s, 0)
            if c:
                lines.append(f"- {SEVERITY_ICONS.get(s, '')} **{s.upper()}**: {c}")
        lines.append("")

    # -- Detailed findings -------------------------------------------------
    for r in results:
        if not r["findings"]:
            continue

        lines.append(f"## {r['check']}\n")

        sorted_findings = sorted(
            r["findings"],
            key=lambda f: SEVERITY_ORDER.index(f.get("severity", "info"))
            if f.get("severity", "info") in SEVERITY_ORDER
            else 99,
        )

        for idx, finding in enumerate(sorted_findings, 1):
            sev = finding.get("severity", "info")
            icon = SEVERITY_ICONS.get(sev, "\u26aa")
            title = finding.get("title", "Finding")
            fp = finding.get("file", "")
            line_no = finding.get("line", 0)

            loc = ""
            if fp:
                loc = f" in `{fp}`"
                if line_no:
                    loc += f" (line {line_no})"

            lines.append(f"### {icon} {idx}. {title}")
            lines.append(
                f"**Severity:** {sev.upper()} · "
                f"**Category:** {finding.get('category', 'general')}"
                f"{loc}\n"
            )
            if finding.get("description"):
                lines.append(f"{finding['description']}\n")
            if finding.get("suggestion"):
                lines.append(f"**\U0001f4a1 Suggestion:** {finding['suggestion']}\n")
            lines.append("---\n")

    if total == 0:
        lines.append("## \u2705 No Issues Found\n")
        lines.append("All checks passed without findings.\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


def _format_json(results: list[dict], config: dict) -> str:
    output = {
        "meta": {
            "tool": "pr-guard-ai",
            "version": "1.0.0",
            "date": datetime.now(timezone.utc).isoformat(),
            "model": config.get("model", "unknown"),
            "checks": config.get("enabled_checks", []),
        },
        "summary": {
            "total_findings": sum(len(r["findings"]) for r in results),
            "by_severity": {},
            "by_check": {},
        },
        "results": results,
    }

    for r in results:
        output["summary"]["by_check"][r["check"]] = len(r["findings"])
        for f in r["findings"]:
            s = f.get("severity", "info")
            output["summary"]["by_severity"][s] = (
                output["summary"]["by_severity"].get(s, 0) + 1
            )

    return json.dumps(output, indent=2, default=str)


# ---------------------------------------------------------------------------
# SARIF formatter  (GitHub Code Scanning compatible)
# ---------------------------------------------------------------------------


def _format_sarif(results: list[dict], config: dict) -> str:
    sev_map = {
        "critical": "error",
        "high": "error",
        "medium": "warning",
        "low": "note",
        "info": "note",
    }

    run = {
        "tool": {
            "driver": {
                "name": "PR Guard AI",
                "version": "1.0.0",
                "informationUri": "https://github.com/dexterite/pr-guard-ai",
                "rules": [],
            }
        },
        "results": [],
    }

    rules_seen: set[str] = set()

    for r in results:
        for finding in r["findings"]:
            cat = finding.get("category", "general")
            rule_id = f"{r['check']}/{cat}"

            if rule_id not in rules_seen:
                rules_seen.add(rule_id)
                run["tool"]["driver"]["rules"].append(
                    {
                        "id": rule_id,
                        "shortDescription": {
                            "text": finding.get("title", cat)
                        },
                    }
                )

            entry: dict = {
                "ruleId": rule_id,
                "level": sev_map.get(finding.get("severity", "info"), "note"),
                "message": {
                    "text": finding.get(
                        "description", finding.get("title", "")
                    )
                },
            }

            fp = finding.get("file", "")
            ln = finding.get("line", 1)
            if fp:
                entry["locations"] = [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": fp},
                            "region": {"startLine": max(1, ln)},
                        }
                    }
                ]

            run["results"].append(entry)

    sarif = {
        "$schema": (
            "https://docs.oasis-open.org/sarif/sarif/v2.1.0/"
            "sarif-schema-2.1.0.json"
        ),
        "version": "2.1.0",
        "runs": [run],
    }

    return json.dumps(sarif, indent=2)


# ---------------------------------------------------------------------------
# Sanitization patterns
# ---------------------------------------------------------------------------

_SENSITIVE_RE = [
    re.compile(
        r"(?i)(api[_-]?key|apikey|secret|password|passwd|token|bearer|auth)"
        r"\s*[:=]\s*[\"']?[\w+/=\-]{8,}"
    ),
    re.compile(
        r"(?i)(aws|azure|gcp|github|slack|sendgrid|twilio)"
        r"[_-]?(key|secret|token)\s*[:=]\s*[\"']?[\w+/=\-]{8,}"
    ),
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    re.compile(r"sk-[A-Za-z0-9]{48}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"xox[bprs]-[A-Za-z0-9\-]+"),
    re.compile(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}"),
]
