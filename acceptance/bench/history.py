"""Grow a stack's finished-task history to a target count by cloning real rows.

    python history.py <total finished tasks>

Each clone copies a succeeded task, its final attempt and its stored input and
result, with new ids and created/finished times shifted into the past. Logs are
not cloned. This is the history recurring queries and indexes see.
"""

from __future__ import annotations

import subprocess
import sys

import sample


def psql(query: str) -> str:
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            sample.POSTGRES,
            "psql",
            "-U",
            "lazycloud",
            "-d",
            "lazycloud",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
        ],
        input=query,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def columns(table: str) -> list[str]:
    return psql(
        f"select column_name from information_schema.columns where table_schema='public' "
        f"and table_name='{table}' order by ordinal_position",
    ).split()


def clone(table: str, alias: str, overrides: dict[str, str], join: str) -> str:
    cols = columns(table)
    exprs = [overrides.get(c, f"{alias}.{c}") for c in cols]
    return f"insert into {table} ({', '.join(cols)}) select {', '.join(exprs)} from {table} {alias} {join};\n"


def main() -> None:
    total = int(sys.argv[1])
    finished = int(psql("select count(*) from tasks where finished_at is not null"))
    source = int(psql("select count(*) from tasks where status = 'succeeded'"))
    copies = max(0, -(-(total - finished) // source))
    shift = "(m.g * interval '1 hour')"
    sql = f"""begin;
create temp table m on commit drop as
  select t.id as old_id, uuidv7() as new_id, g from tasks t, generate_series(1, {copies}) g
  where t.status = 'succeeded';
"""
    times = {
        "created_at": f"t.created_at - {shift}",
        "started_at": f"t.started_at - {shift}",
        "finished_at": f"t.finished_at - {shift}",
        "parent_task_id": "null",
        "root_task_id": "null",
    }
    sql += clone(
        "tasks",
        "t",
        {"id": "m.new_id", "current_attempt_id": "null", **times},
        "join m on m.old_id = t.id",
    )
    sql += """create temp table am on commit drop as
  select a.id as old_id, uuidv7() as new_id, m.new_id as task_id, m.g from attempts a
  join tasks t on t.current_attempt_id = a.id join m on m.old_id = t.id;
"""
    sql += clone(
        "attempts",
        "a",
        {
            "id": "am.new_id",
            "task_id": "am.task_id",
            "started_at": "a.started_at - (am.g * interval '1 hour')",
            "finished_at": "a.finished_at - (am.g * interval '1 hour')",
            "deadline_at": "a.deadline_at - (am.g * interval '1 hour')",
        },
        "join am on am.old_id = a.id",
    )
    sql += "update tasks t set current_attempt_id = am.new_id from am where am.task_id = t.id;\n"
    for table in ("task_inputs", "task_results"):
        sql += clone(table, "x", {"task_id": "m.new_id"}, "join m on m.old_id = x.task_id")
    sql += "commit;\nanalyze tasks;\n"
    psql(sql)
    print(
        "finished tasks:", psql("select count(*) from tasks where finished_at is not null").strip()
    )


if __name__ == "__main__":
    main()
