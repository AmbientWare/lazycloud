# Database

- Own PostgreSQL tables, constraints, queries and Alembic migrations; services own
  workflows. Enforce relational invariants in the schema wherever possible.
- Migrations use explicit DDL, never live metadata `create_all`. Keep metadata
  and migration schema equivalent, including extensions, indexes and constraints.
  Add new revisions for changes; never edit deployed history.
- Break foreign-key dependency cycles with explicit deferred table construction
  and `use_alter`.
- Billing boundaries are caller-supplied `timestamptz` instants, independent of
  session timezone. Ledger segments are append-only; their component defines the
  quantity unit. Do not duplicate derived breakdowns or reprice recorded usage.
- Enforce active machine-name uniqueness per owner in SQL. Preserve deleted names
  as history and explicit machine/workspace links.
