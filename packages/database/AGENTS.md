# Database Package

PostgreSQL, SQLAlchemy, and Alembic—the platform's durable state.

Use explicit relational tables and constraints. Put an invariant in the schema
wherever the schema can hold it: a rule enforced only in Python is enforced only
where someone remembered to call it. Repositories map and query; services decide.
Redis hot state stays out unless the history itself has to be durable.

While predeployment, schema authority is the SQLAlchemy metadata plus one
reviewed baseline migration. Update that baseline and recreate local development
databases freely rather than accumulating historical revisions or upgrade paths.
Never reset external, deployed, or production data.
