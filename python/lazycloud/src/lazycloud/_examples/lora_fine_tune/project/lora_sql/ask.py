"""Ask the deployed compare endpoint a question and print both answers.

uv run python -m lora_sql.ask "Which customers spent more than 100 in total?"
"""

import argparse
from pathlib import Path

from .compare import Answer, Comparison, compare

SAMPLE_DATABASE = Path(__file__).with_name("shop.sql")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the base and tuned answers.")
    parser.add_argument("question")
    parser.add_argument(
        "--database",
        type=Path,
        default=SAMPLE_DATABASE,
        help="a file of CREATE TABLE and INSERT statements",
    )
    args = parser.parse_args()
    response = compare.request(
        question=args.question, context=args.database.read_text(encoding="utf-8")
    )
    if response.status_code != 200:
        raise SystemExit(f"compare returned {response.status_code}: {response.text}")
    comparison = Comparison.model_validate(response.json())
    for label, answer in (("base", comparison.base), ("tuned", comparison.tuned)):
        print(f"--- {label}\n{answer.sql}\n{_outcome(answer)}\n")


def _outcome(answer: Answer) -> str:
    if answer.result is None:
        return f"error: {answer.error}"
    rows = "\n".join(" | ".join(str(cell) for cell in row) for row in answer.result.rows)
    return f"{' | '.join(answer.result.columns)}\n{rows}"


if __name__ == "__main__":
    main()
