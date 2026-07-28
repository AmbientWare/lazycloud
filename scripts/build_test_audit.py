"""Generate `test-audit.md`, the checklist backing the one-off test audit.

Every collected pytest id, opt-in E2E module and web test file gets a row, so an
auditor records a verdict against the real inventory rather than a sample. Run
from the repository root:

    uv run python scripts/build_test_audit.py
"""

from __future__ import annotations

import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
AUDIT_FILE = REPOSITORY_ROOT / "test-audit.md"

HEADER = """# Test audit

One-off audit of every test in the repository against the Test Decision Gate in
`CLAUDE.md`. Each row carries a verdict:

- `keep` — proves current production behaviour at a stable owner or public
  boundary, and passes all four gate criteria.
- `delete` — fails the gate: tests machinery, implementation shape, a removed
  capability, or an invariant already proven at a cheaper authoritative owner.
- `update` — the invariant is worth proving but the test states it wrongly.
  Record why, and how.

Mark a row `- [x]` once its verdict is recorded. Verdicts go in the `verdict`
column; `update` rows carry their reasoning inline.

| owner | count |
|---|---|
"""


def _collected_ids() -> list[str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return [line.strip() for line in result.stdout.splitlines() if "::" in line]


def _owner(path: str) -> str:
    parts = Path(path).parts
    if parts[0] in {"packages", "apps"} and len(parts) > 1:
        if parts[1] == "providers" and len(parts) > 2:
            return f"{parts[0]}/{parts[1]}/{parts[2]}"
        return f"{parts[0]}/{parts[1]}"
    return parts[0]


def main() -> int:
    by_file: dict[str, list[str]] = defaultdict(list)
    for test_id in _collected_ids():
        path, _, name = test_id.partition("::")
        by_file[path].append(name)

    e2e_modules = sorted(
        str(path.relative_to(REPOSITORY_ROOT))
        for path in (REPOSITORY_ROOT / "tests" / "e2e").rglob("*.py")
        if "__pycache__" not in path.parts
    )
    web_tests = sorted(
        str(path.relative_to(REPOSITORY_ROOT))
        for pattern in ("*.test.ts", "*.test.tsx", "*.spec.ts")
        for path in (REPOSITORY_ROOT / "apps" / "web").rglob(pattern)
        if "node_modules" not in path.parts and ".output" not in path.parts
    )

    by_owner: dict[str, int] = defaultdict(int)
    for path, names in by_file.items():
        by_owner[_owner(path)] += len(names)

    lines = [HEADER]
    for owner in sorted(by_owner):
        lines.append(f"| `{owner}` | {by_owner[owner]} |\n")
    total = sum(by_owner.values())
    lines.append(f"| **pytest total** | **{total}** |\n")
    lines.append(f"| **opt-in E2E modules** | **{len(e2e_modules)}** |\n")
    lines.append(f"| **web test files** | **{len(web_tests)}** |\n")

    lines.append("\n---\n\n## pytest\n")
    for path in sorted(by_file):
        lines.append(f"\n### `{path}`\n\n")
        for name in by_file[path]:
            lines.append(f"- [ ] `{name}` — verdict:\n")

    lines.append("\n---\n\n## Opt-in E2E (`tests/e2e`)\n\n")
    lines.append(
        "Not collected by pytest. Scenarios are production capability proofs, not\n"
        "unit tests: judge each on whether it proves a distinct user-visible\n"
        "capability through a public surface and cleans up after itself.\n\n"
    )
    for path in e2e_modules:
        lines.append(f"- [ ] `{path}` — verdict:\n")

    lines.append("\n---\n\n## Web (`apps/web`)\n\n")
    for path in web_tests:
        lines.append(f"- [ ] `{path}` — verdict:\n")

    AUDIT_FILE.write_text("".join(lines), encoding="utf-8")
    print(f"wrote {AUDIT_FILE.relative_to(REPOSITORY_ROOT)}")
    print(f"  pytest tests: {total} across {len(by_file)} files")
    print(f"  e2e modules:  {len(e2e_modules)}")
    print(f"  web files:    {len(web_tests)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
