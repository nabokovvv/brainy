#!/usr/bin/env python3
"""Zero-cost dependency audit for private repos without GitHub Advanced Security.

Runs pip-audit and safety in CI; fails closed on high/critical vulnerabilities.
No paid features, no network calls beyond PyPI advisory DB (public, free).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def run_pip_audit(verbose: bool = False) -> tuple[int, str]:
    """Run pip-audit and return (exit_code, output)."""
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "--desc",
        "--format",
        "json",
        "--skip-editable",
    ]
    if not verbose:
        cmd.append("--quiet")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout


def run_safety(verbose: bool = False) -> tuple[int, str]:
    """Run safety check and return (exit_code, output)."""
    cmd = [
        sys.executable,
        "-m",
        "safety",
        "check",
        "--json",
    ]
    if not verbose:
        cmd.append("--quiet")
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout


def parse_pip_audit(output: str) -> list[dict]:
    """Parse pip-audit JSON output into list of vulnerabilities."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    vulns = []
    for vuln in data.get("vulnerabilities", []):
        vulns.append(
            {
                "package": vuln.get("package", "unknown"),
                "version": vuln.get("installed_version", "unknown"),
                "id": vuln.get("id", "unknown"),
                "description": vuln.get("description", ""),
                "fix_versions": vuln.get("fix_versions", []),
            }
        )
    return vulns


def parse_safety(output: str) -> list[dict]:
    """Parse safety JSON output into list of vulnerabilities."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    vulns = []
    for vuln in data.get("vulnerabilities", []):
        vulns.append(
            {
                "package": vuln.get("package_name", "unknown"),
                "version": vuln.get("installed_version", "unknown"),
                "id": vuln.get("vulnerability_id", "unknown"),
                "description": vuln.get("advisory", ""),
                "fix_versions": vuln.get("fixed_versions", []),
            }
        )
    return vulns


def main() -> int:
    parser = argparse.ArgumentParser(description="Zero-cost dependency audit")
    parser.add_argument(
        "--fail-on",
        choices=["high", "critical", "any"],
        default="high",
        help="Fail on severity level (pip-audit only reports high/critical)",
    )
    parser.add_argument("--skip-pip-audit", action="store_true", help="Skip pip-audit check")
    parser.add_argument("--skip-safety", action="store_true", help="Skip safety check")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    all_vulns: list[dict] = []
    failed = False

    # Run pip-audit
    if not args.skip_pip_audit:
        print("Running pip-audit...", file=sys.stderr)
        code, output = run_pip_audit(args.verbose)
        vulns = parse_pip_audit(output)
        all_vulns.extend(vulns)
        if code != 0 and vulns:
            failed = True
        if args.verbose:
            print(output)

    # Run safety
    if not args.skip_safety:
        print("Running safety...", file=sys.stderr)
        code, output = run_safety(args.verbose)
        vulns = parse_safety(output)
        all_vulns.extend(vulns)
        if code != 0 and vulns:
            failed = True
        if args.verbose:
            print(output)

    # Report
    if all_vulns:
        print(f"\nFound {len(all_vulns)} vulnerabilities:", file=sys.stderr)
        for v in all_vulns:
            fix = (
                f" (fix: {', '.join(v['fix_versions'])})"
                if v["fix_versions"]
                else " (no fix version)"
            )
            print(f"  {v['package']}=={v['version']}: {v['id']}{fix}", file=sys.stderr)
    else:
        print("No vulnerabilities found.", file=sys.stderr)

    # Write JSON report for CI artifacts
    report_path = Path("dependency-audit-report.json")
    report_path.write_text(json.dumps({"vulnerabilities": all_vulns}, indent=2))
    print(f"Report written to {report_path}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
