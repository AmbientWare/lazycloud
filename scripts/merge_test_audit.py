"""Fold audited verdicts into `test-audit.md`.

Reads the audit result JSON produced by the audit run and writes each verdict
onto its row, so the checklist records the decision beside the test rather than
in a separate report. Run from the repository root:

    uv run python scripts/merge_test_audit.py <audit.json>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUDIT_FILE = REPOSITORY_ROOT / "test-audit.md"

_HEADING = re.compile(r"^### `(?P<path>.+)`$")
_ROW = re.compile(r"^- \[ \] `(?P<name>.+?)` — verdict:$")


def main(result_path: str) -> int:
    data = json.loads(Path(result_path).read_text(encoding="utf-8"))
    verdicts: dict[str, dict[str, str]] = {}
    for finding in data["findings"]:
        verdicts[finding["id"]] = finding
    bugs = {bug["id"]: bug for bug in data.get("production_bugs", [])}

    lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    current = ""
    counts = {"keep": 0, "delete": 0, "update": 0}

    for line in lines:
        heading = _HEADING.match(line)
        if heading:
            current = heading.group("path")
            output.append(line)
            continue
        row = _ROW.match(line)
        if row is None:
            output.append(line)
            continue
        name = row.group("name")
        # E2E and web rows carry a path in place of a test name.
        test_id = f"{current}::{name}" if current and "::" not in name else name
        if test_id not in verdicts and name.endswith((".py", ".ts", ".tsx")):
            test_id = name
        finding = verdicts.get(test_id)
        bug = bugs.get(test_id)
        if finding is None:
            counts["keep"] += 1
            entry = f"- [x] `{name}` — verdict: keep"
        else:
            counts[finding["verdict"]] += 1
            entry = f"- [x] `{name}` — verdict: **{finding['verdict']}** — {finding['reason']}"
            how = finding.get("how") or ""
            if how:
                entry += f"\n  - **How:** {how}"
        if bug is not None:
            entry += f"\n  - **PRODUCTION BUG:** {bug['detail']}"
        output.append(entry)

    AUDIT_FILE.write_text("\n".join(output) + "\n", encoding="utf-8")
    print(f"merged into {AUDIT_FILE.relative_to(REPOSITORY_ROOT)}")
    print(f"  keep {counts['keep']}  delete {counts['delete']}  update {counts['update']}")
    unmatched = sorted(set(verdicts) - {*()})
    matched = counts["delete"] + counts["update"]
    if matched != len(verdicts):
        print(f"  WARNING: {len(verdicts) - matched} verdicts did not match a row")
        for test_id in unmatched[:10]:
            print(f"    unmatched candidate: {test_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
