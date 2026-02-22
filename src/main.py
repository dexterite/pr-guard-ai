#!/usr/bin/env python3
"""PR Guard AI - Main entry point for AI-powered code review."""

import os
import sys
import time

# Allow imports from the src directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_loader import load_config
from runner import CheckRunner
from output_formatter import format_report, sanitize_output
from shipper import ship_results

SEVERITY_ORDER = ["info", "low", "medium", "high", "critical"]


def main():
    print("=" * 60)
    print(" PR Guard AI - AI-Powered Code Review")
    print("=" * 60)

    start_time = time.time()

    # ------------------------------------------------------------------
    # 1. Configuration
    # ------------------------------------------------------------------
    print("\n[1/5] Loading configuration...")
    config = load_config()

    print(f"\n  Model         : {config['model']}")
    print(f"  API base      : {_mask_url(config['api_base_url'])}")
    print(f"  Checks        : {', '.join(config['enabled_checks'])}")
    if config.get("full_scan"):
        print(f"  Scan mode     : FULL SCAN (all tracked files)")
    else:
        print(f"  Diff only     : {config['diff_only']}")
    print(f"  Threshold     : {config['severity_threshold']}")
    print(f"  Output format : {config['output_format']}")
    print(f"  Ship to       : {config['ship_to']}")
    print(f"  Max file KB   : {config['max_file_size_kb']}")
    delay = config.get('request_delay_ms', 0)
    if delay:
        print(f"  Request delay : {delay}ms (+ adaptive on 429)")
    else:
        print(f"  Request delay : adaptive only (ramps on 429)")
    print(f"  Debug         : {config.get('debug', False)}")

    if not config["enabled_checks"]:
        print("\n::warning::No checks enabled. Nothing to do.")
        _set_outputs(0, 0, "", 0)
        return

    # ------------------------------------------------------------------
    # 2. Run checks
    # ------------------------------------------------------------------
    checks = config['enabled_checks']
    print(f"\n[2/5] Running {len(checks)} check(s): {', '.join(checks)}...")
    runner = CheckRunner(config)
    results = runner.run()

    # ------------------------------------------------------------------
    # 3. Format & sanitize
    # ------------------------------------------------------------------
    print(f"\n[3/5] Formatting report ({config['output_format']})...")
    report = format_report(results, config)
    print("[4/5] Sanitizing output...")
    safe_report = sanitize_output(report, config)

    # ------------------------------------------------------------------
    # 4. Ship results
    # ------------------------------------------------------------------
    print(f"[5/5] Shipping results to {config['ship_to']}...")
    report_path = ship_results(safe_report, results, config)

    # ------------------------------------------------------------------
    # 5. Summary & outputs
    # ------------------------------------------------------------------
    elapsed = time.time() - start_time
    total_findings = sum(len(r["findings"]) for r in results)
    files_analyzed = sum(r["files_analyzed"] for r in results)

    # Per-check breakdown
    print("\n  Per-check results:")
    for r in results:
        fc = r["files_analyzed"]
        fn = len(r["findings"])
        print(f"    {r['check']:>20} : {fc} file(s), {fn} finding(s)")

    by_severity = {}
    for r in results:
        for f in r["findings"]:
            sev = f.get("severity", "info")
            by_severity[sev] = by_severity.get(sev, 0) + 1

    critical = by_severity.get("critical", 0)
    high = by_severity.get("high", 0)

    print(f"\n{'=' * 60}")
    print(f" PR Guard AI - Complete ({elapsed:.1f}s)")
    print(f"  Files analyzed : {files_analyzed}")
    print(f"  Total findings : {total_findings}")
    for sev in reversed(SEVERITY_ORDER):
        count = by_severity.get(sev, 0)
        if count:
            print(f"    {sev:>8} : {count}")
    print(f"{'=' * 60}")

    # Determine exit code based on severity threshold
    should_fail = _above_threshold(results, config["severity_threshold"])
    exit_code = 1 if should_fail else 0

    _set_outputs(total_findings, critical, report_path, exit_code)

    if should_fail:
        print(
            f"\n::error::Findings at or above severity threshold "
            f"'{config['severity_threshold']}' detected."
        )
        sys.exit(1)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _above_threshold(results, threshold):
    """Return True if any finding meets or exceeds the severity threshold."""
    if threshold not in SEVERITY_ORDER:
        return False
    threshold_idx = SEVERITY_ORDER.index(threshold)
    for r in results:
        for f in r["findings"]:
            sev = f.get("severity", "info")
            if sev in SEVERITY_ORDER and SEVERITY_ORDER.index(sev) >= threshold_idx:
                return True
    return False


def _set_outputs(findings_count, critical_count, report_path, exit_code):
    """Write to $GITHUB_OUTPUT for downstream steps."""
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if output_file:
        try:
            with open(output_file, "a") as fh:
                fh.write(f"findings-count={findings_count}\n")
                fh.write(f"critical-count={critical_count}\n")
                fh.write(f"report-path={report_path}\n")
                fh.write(f"exit-code={exit_code}\n")
        except OSError as exc:
            print(f"::warning::Could not write GITHUB_OUTPUT: {exc}")


def _mask_url(url):
    """Return a URL with the host visible but nothing else leaked."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.hostname}/..."
    except Exception:
        return "(configured)"


if __name__ == "__main__":
    main()
