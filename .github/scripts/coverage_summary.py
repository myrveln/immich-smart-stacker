#!/usr/bin/env python3
"""Write a short coverage summary to the GitHub Actions step summary.

Designed for CI use in `.github/workflows/test.yml`.

Usage:
  python .github/scripts/coverage_summary.py [path/to/coverage.xml]

Writes markdown to $GITHUB_STEP_SUMMARY if set; otherwise prints to stdout.
"""

from __future__ import annotations

import os
import pathlib
import sys
import xml.etree.ElementTree as ET


def _write_summary(text: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        pathlib.Path(summary_path).write_text(text, encoding="utf-8")
    else:
        print(text)


def main(argv: list[str]) -> int:
    coverage_xml = pathlib.Path(argv[1] if len(argv) > 1 else "tests/coverage.xml")

    if not coverage_xml.exists():
        _write_summary("## Coverage\n\n- No `tests/coverage.xml` found; skipping summary.\n")
        return 0

    root = ET.parse(coverage_xml).getroot()
    line_rate = float(root.attrib.get("line-rate", "0"))
    percent = line_rate * 100

    repo = os.environ.get("GITHUB_REPOSITORY", "myrveln/immich-smart-stacker")
    codecov_url = f"https://codecov.io/gh/{repo}"

    _write_summary(
        "## Coverage\n\n"
        f"- Line coverage: **{percent:.2f}%**\n"
        f"- Codecov: {codecov_url}\n"
    )

    print(f"Line coverage: {percent:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
