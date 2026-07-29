# Database Package

PostgreSQL/SQLAlchemy/Alembic is durable state. Use explicit relational tables,
constraints, and repositories; repositories map/query while services decide.
Keep Redis hot state out unless durable history is required.

Predeployment schema authority is metadata plus one reviewed Alembic baseline.
Schema changes prove fresh PostgreSQL bootstrap, metadata parity, constraints,
mapping, transactions/isolation, and the affected owner workflow. Recreate and
re-bootstrap local development databases freely when a baseline changes; do not
add historical migrations or transition tests. Never reset external, deployed,
or production data.
