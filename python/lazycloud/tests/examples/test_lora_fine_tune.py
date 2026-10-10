from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

import lazycloud

sys.path.insert(
    0, str(Path(lazycloud.__file__).parent / "_examples" / "lora_fine_tune" / "project")
)

from lora_sql.data import SqlExample, usable_examples
from lora_sql.evaluate import Predictions, build_report
from lora_sql.prompt import extract_sql
from lora_sql.queries import MAX_ROWS, QueryError, SchemaError, execution_match, run_query
from lora_sql.resources import run_dir
from lora_sql.train import TrainingConfig, latest_checkpoint, record_settings

SALES = (
    "CREATE TABLE sales (id INT, region TEXT, amount REAL);"
    "INSERT INTO sales VALUES (1, 'north', 10), (2, 'south', 30), (3, 'north', 20);"
)


def test_model_replies_yield_their_first_statement() -> None:
    reply = "Here is the query:\n```sql\nSELECT 1;\nSELECT 2;\n```\nIt selects one."
    assert extract_sql(reply) == "SELECT 1;"
    assert extract_sql("SELECT ';' AS sep; trailing words") == "SELECT ';' AS sep;"
    assert extract_sql("  SELECT region FROM sales  ") == "SELECT region FROM sales"


def test_a_query_matches_on_rows_not_column_names_or_number_types() -> None:
    reference = "SELECT region, SUM(amount) FROM sales GROUP BY region;"
    assert execution_match(
        SALES, reference, "SELECT region AS r, SUM(amount) AS total FROM sales GROUP BY 1;"
    )
    assert execution_match(SALES, "SELECT COUNT(*) FROM sales;", "SELECT 3.0;")
    assert not execution_match(
        SALES, reference, "SELECT region, MAX(amount) FROM sales GROUP BY 1;"
    )
    assert not execution_match(SALES, reference, "SELECT nonsense FROM sales;")


def test_row_order_counts_only_when_the_reference_orders_its_result() -> None:
    assert execution_match(SALES, "SELECT id FROM sales;", "SELECT id FROM sales ORDER BY id DESC;")
    assert not execution_match(
        SALES, "SELECT id FROM sales ORDER BY amount;", "SELECT id FROM sales ORDER BY id;"
    )
    window = "SELECT id, RANK() OVER (ORDER BY amount) FROM sales;"
    assert execution_match(SALES, window, f"{window[:-1]} ORDER BY id DESC;")


def test_queries_cannot_reach_files_or_run_forever(tmp_path: Path) -> None:
    attach = f"ATTACH DATABASE '{tmp_path / 'stolen.db'}' AS other;"
    with pytest.raises(SchemaError):
        run_query(attach + SALES, "SELECT 1;")
    with pytest.raises(QueryError):
        run_query(SALES, attach)
    assert not (tmp_path / "stolen.db").exists()

    endless = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT max(x) FROM n;"
    )
    started = time.monotonic()
    with pytest.raises(QueryError):
        run_query(SALES, endless, seconds=0.2)
    assert time.monotonic() - started < 2


def test_large_results_are_truncated() -> None:
    many = (
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n LIMIT 500) SELECT x FROM n;"
    )
    result = run_query(SALES, many)
    assert len(result.rows) == MAX_ROWS
    assert result.truncated


def test_training_data_keeps_sqlite_queries_that_return_data() -> None:
    def row(
        sql: str, task: str = "analytics and reporting", context: str = SALES
    ) -> dict[str, str]:
        return {
            "sql_prompt": "question",
            "sql_context": context,
            "sql": sql,
            "sql_task_type": task,
            "sql_complexity": "basic SQL",
        }

    rows = [
        row("SELECT SUM(amount) FROM sales;"),
        row("DELETE FROM sales WHERE id = 1;", task="data manipulation"),
        row("SELECT DATE_SUB(NOW(), INTERVAL 1 DAY);"),
        row("SELECT amount FROM sales WHERE region = 'east';"),
        row("SELECT AVG(amount) FROM sales WHERE region = 'east';"),
        row("SELECT 1;", context="CREATE TABLE broken (;"),
        row("SELECT 1;", context="x" * 5000),
    ]
    assert [example.sql for example in usable_examples(rows)] == ["SELECT SUM(amount) FROM sales;"]


def test_run_names_stay_inside_the_runs_directory() -> None:
    assert run_dir("sql-v2").name == "sql-v2"
    for name in ("../base-model", "Sql", "", "a/b"):
        with pytest.raises(ValueError):
            run_dir(name)


def test_resume_picks_the_newest_complete_checkpoint(tmp_path: Path) -> None:
    for step, complete in ((50, True), (100, True), (150, False)):
        checkpoint = tmp_path / f"checkpoint-{step}"
        checkpoint.mkdir()
        if complete:
            (checkpoint / "trainer_state.json").write_text("{}")
    (tmp_path / "checkpoint-tmp").mkdir()
    assert latest_checkpoint(tmp_path) == tmp_path / "checkpoint-100"
    assert latest_checkpoint(tmp_path / "missing") is None


def test_a_run_resumes_only_with_its_original_settings(tmp_path: Path) -> None:
    settings = tmp_path / "sql-v1" / "settings.json"
    record_settings(settings, TrainingConfig(lora_rank=16))
    record_settings(settings, TrainingConfig(lora_rank=16))
    with pytest.raises(ValueError, match="new run name"):
        record_settings(settings, TrainingConfig(lora_rank=32))


def test_training_settings_are_bounded() -> None:
    with pytest.raises(ValueError):
        TrainingConfig.model_validate({"lora_rank": 128})
    with pytest.raises(ValueError):
        TrainingConfig.model_validate({"learning_rate": 0.1})
    with pytest.raises(ValueError):
        TrainingConfig.model_validate({"epoch": 2})


def test_the_report_breaks_accuracy_down_by_complexity() -> None:
    examples = [
        SqlExample(question=f"q{n}", context=SALES, sql="SELECT 1;", complexity=complexity)
        for n, complexity in enumerate(["basic SQL", "basic SQL", "single join"])
    ]
    predictions = Predictions(base=["SELECT 2;"] * 3, tuned=["SELECT 1;"] * 3)
    report = build_report(
        "sql-v1", examples, predictions, [True, False, False], [True, True, False]
    )
    assert "| All | 3 | 33.3% | 66.7% |" in report
    assert "| basic SQL | 2 | 50.0% | 100.0% |" in report
    assert "| single join | 1 | 0.0% | 0.0% |" in report
    assert "**q1**" in report.split("## Fixed by the adapter")[1].split("## Still wrong")[0]
