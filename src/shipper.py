"""Ship results to one or more destinations.

Supported targets (via the ``ship-to`` input, comma-separated):
    github-summary   — write to $GITHUB_STEP_SUMMARY  (default)
    file             — write to a local file
    webhook          — POST JSON to a URL
    github-pr-comment — post a comment on the PR (requires github-token)

The ``ship_results`` function returns the path of the written report file
(empty string if no file was written).

Extensibility
-------------
To add a custom shipper, create a Python module that exposes a
``ship(report, results, config)`` function and invoke it from a
post-step in your workflow.  The webhook destination can also be
used as a generic integration point for any external system.
"""

import json
import os

import requests


def ship_results(report: str, results: list[dict], config: dict) -> str:
    """Dispatch the report to every configured destination.

    Returns the path of the report file if ``file`` is one of the
    destinations, otherwise ``""``.
    """
    destinations = [d.strip() for d in config.get("ship_to", "github-summary").split(",")]
    report_path = ""

    for dest in destinations:
        if dest == "github-summary":
            _to_github_summary(report)
        elif dest == "file":
            report_path = _to_file(report, config)
        elif dest == "webhook":
            _to_webhook(report, results, config)
        elif dest == "github-pr-comment":
            _to_pr_comment(report, config)
        else:
            print(f"::warning::Unknown ship destination '{dest}' — skipped")

    return report_path


# ---------------------------------------------------------------------------
# Destinations
# ---------------------------------------------------------------------------


def _to_github_summary(report: str) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        try:
            with open(summary_file, "a", encoding="utf-8") as fh:
                fh.write(report)
                fh.write("\n")
            print("  → Shipped to GitHub Actions step summary")
        except OSError as exc:
            print(f"::warning::Could not write step summary: {exc}")
    else:
        # Outside of GitHub Actions — print to stdout
        print(report)


def _to_file(report: str, config: dict) -> str:
    ext_map = {"markdown": "md", "json": "json", "sarif": "sarif.json"}
    fmt = config.get("output_format", "markdown")
    ext = ext_map.get(fmt, "txt")

    base = config.get("ship_file_path", "pr-guard-report")
    file_path = f"{base}.{ext}"

    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(report)

    print(f"  → Report written to {file_path}")
    return file_path


def _to_webhook(report: str, results: list[dict], config: dict) -> None:
    url = config.get("ship_webhook_url", "")
    if not url:
        print("::warning::ship-to includes 'webhook' but ship-webhook-url is empty")
        return

    payload = {
        "source": "pr-guard-ai",
        "repository": os.environ.get("GITHUB_REPOSITORY", ""),
        "ref": os.environ.get("GITHUB_REF", ""),
        "sha": os.environ.get("GITHUB_SHA", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "total_findings": sum(len(r["findings"]) for r in results),
        "results": results,
        "report": report[:50_000],  # cap payload size
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json", "User-Agent": "pr-guard-ai/1.0"},
            timeout=30,
        )
        if resp.status_code < 300:
            print(f"  → Shipped to webhook ({resp.status_code})")
        else:
            print(f"::warning::Webhook returned {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        print(f"::warning::Webhook delivery failed: {exc}")


def _to_pr_comment(report: str, config: dict) -> None:
    token = config.get("github_token", "")
    if not token:
        print("::warning::github-token not provided — skipping PR comment")
        return

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    pr_number = _extract_pr_number()

    if not pr_number or not repo:
        print("::warning::Not in a PR context — skipping PR comment")
        return

    # GitHub comment limit is ~65 536 chars
    MAX_BODY = 60_000
    body = report
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY] + "\n\n_… report truncated_"

    body = (
        "<details>\n<summary>\U0001f6e1\ufe0f PR Guard AI Report</summary>\n\n"
        + body
        + "\n\n</details>"
    )

    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        resp = requests.post(url, json={"body": body}, headers=headers, timeout=30)
        if resp.status_code < 300:
            print(f"  → Posted PR comment on #{pr_number}")
        else:
            print(f"::warning::PR comment failed ({resp.status_code}): {resp.text[:200]}")
    except Exception as exc:
        print(f"::warning::PR comment failed: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_pr_number() -> str:
    """Extract the pull-request number from GITHUB_REF (``refs/pull/N/merge``)."""
    ref = os.environ.get("GITHUB_REF", "")
    if "/pull/" in ref:
        parts = ref.split("/")
        try:
            return parts[parts.index("pull") + 1]
        except (ValueError, IndexError):
            pass
    return ""
