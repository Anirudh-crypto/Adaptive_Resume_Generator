"""Render pytest's JUnit XML into a GitHub step summary and workflow outputs.

Deliberately dependency-free stdlib, and deliberately not a third-party junit-reporting action:
those want a `checks: write` token, which is unavailable to pull requests from forks -- so they
fail exactly where an outside contributor most needs to see why their PR is red.

Usage (quote the glob so Python expands it, not the shell -- a missing reports/ directory then
yields "0 passed" instead of an error):

    python .github/scripts/junit_summary.py "reports/junit-*.xml"
"""

from __future__ import annotations

import glob
import os
import sys
import xml.etree.ElementTree as ET

totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
failed: list[str] = []

for pattern in sys.argv[1:]:
    for path in sorted(glob.glob(pattern)):
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            print(f"::warning::could not parse {path}: {exc}")
            continue
        for suite in root.iter("testsuite"):
            for key in totals:
                totals[key] += int(suite.get(key, 0))
        for case in root.iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failed.append(f"{case.get('classname')}::{case.get('name')}")

passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
line = (
    f"{passed} passed, {totals['failures']} failed, {totals['errors']} errored, "
    f"{totals['skipped']} skipped ({totals['tests']} collected)"
)

if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write(f"## Test results\n\n{line}\n")
        if failed:
            handle.write("\n### Failed\n\n")
            handle.writelines(f"- `{name}`\n" for name in failed)

if output_path := os.environ.get("GITHUB_OUTPUT"):
    with open(output_path, "a", encoding="utf-8") as handle:
        # `line` is single-line by construction, so no heredoc delimiter is needed. The failed
        # list is truncated because GITHUB_OUTPUT values are size-capped and this only feeds a
        # summary line -- the full detail is in the job log and the uploaded artifact.
        handle.write(f"summary={line}\n")
        handle.write(f"failed={' '.join(failed)[:900]}\n")

print(line)
