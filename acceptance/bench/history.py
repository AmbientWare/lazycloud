"""Grow a stack's finished-task history to a target count by cloning real rows.

    python history.py ref|new <total finished tasks>

Each clone copies a succeeded task, its final attempt and, in the rewrite, its
stored input and result, with new ids and created/finished times shifted into
the past. Logs are not cloned. Both platforms keep these rows in PostgreSQL, so
this is the history their recurring queries and indexes see.
"""

from __future__ import annotations

import subprocess
import sys

import sample


def psql(target: str, query: str) -> str:
    return subprocess.run(
        [
            "docker",
            "exec",
            "-i",
            f"{sample.PROJECT[target]}-postgres-1",
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


def columns(target: str, table: str) -> list[str]:
    return psql(
        target,
        f"select column_name from information_schema.columns where table_schema='public' "
        f"and table_name='{table}' order by ordinal_position",
    ).split()


def clone(target: str, table: str, alias: str, overrides: dict[str, str], join: str) -> str:
    cols = columns(target, table)
    exprs = [overrides.get(c, f"{alias}.{c}") for c in cols]
    return f"insert into {table} ({', '.join(cols)}) select {', '.join(exprs)} from {table} {alias} {join};\n"


def main() -> None:
    target, total = sys.argv[1], int(sys.argv[2])
    ok = "succeeded" if target == "new" else "complete"
    finished = int(psql(target, "select count(*) from tasks where finished_at is not null"))
    source = int(psql(target, f"select count(*) from tasks where status = '{ok}'"))
    copies = max(0, -(-(total - finished) // source))
    new_id = "uuidv7()" if target == "new" else "gen_random_uuid()"
    shift = "(m.g * interval '1 hour')"
    sql = f"""begin;
create temp table m on commit drop as
  select t.id as old_id, {new_id} as new_id, g from tasks t, generate_series(1, {copies}) g
  where t.status = '{ok}';
"""
    times = {
        "created_at": f"t.created_at - {shift}",
        "started_at": f"t.started_at - {shift}",
        "finished_at": f"t.finished_at - {shift}",
        "parent_task_id": "null",
        "root_task_id": "null",
    }
    if target == "new":
        sql += clone(
            target,
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
            target,
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
        sql += (
            "update tasks t set current_attempt_id = am.new_id from am where am.task_id = t.id;\n"
        )
        for table in ("task_inputs", "task_results"):
            sql += clone(
                target, table, "x", {"task_id": "m.new_id"}, "join m on m.old_id = x.task_id"
            )
    else:
        sql += clone(
            target,
            "tasks",
            "t",
            {
                "id": "m.new_id",
                "container_id": "null",
                "input_container_id": "null",
                "updated_at": f"t.updated_at - {shift}",
                **times,
            },
            "join m on m.old_id = t.id",
        )
        sql += clone(
            target,
            "task_attempts",
            "a",
            {
                "id": "gen_random_uuid()",
                "task_id": "m.new_id",
                "claim_id": "null",
                "created_at": f"a.created_at - {shift}",
                "updated_at": f"a.updated_at - {shift}",
                "started_at": f"a.started_at - {shift}",
                "finished_at": f"a.finished_at - {shift}",
            },
            "join tasks t on t.id = a.task_id and a.attempt_number = t.attempt_number join m on m.old_id = t.id",
        )
    sql += "commit;\nanalyze tasks;\n"
    psql(target, sql)
    print(
        target,
        "finished tasks:",
        psql(target, "select count(*) from tasks where finished_at is not null").strip(),
    )


if __name__ == "__main__":
    main()
