# Database package

PostgreSQL, SQLAlchemy, and Alembic: the platform's durable state.

Use explicit relational tables and constraints. Put an invariant in the schema
wherever the schema can hold it: a rule enforced only in Python is enforced only
where someone remembered to call it. Repositories map and query; services decide.
Redis hot state stays out unless the history itself has to be durable.

While predeployment, schema authority is the SQLAlchemy metadata plus one
reviewed baseline migration. Update that baseline and recreate local development
databases freely rather than accumulating historical revisions or upgrade paths.
Never reset external, deployed, or production data.

A PostgreSQL extension the schema needs is declared with the metadata, in
`tables/base.py`, not in the migration. Every path that builds the schema needs
it, both the baseline and a test that calls `create_all`, and an extension named
in only one of them is a schema that cannot be created by the other.

A ledger segment states one component's quantity, and whether that quantity is
capacity held or capacity measured. The component is what decides which unit the
quantity counts, so the unit is not a column: stored beside the quantity it is a
second statement of something the component already fixes, and two statements of
one fact are two things that can disagree. There is no second breakdown beside
it either, in a column or in a response field: a resource total is that
component's rows summed, and a copy of the same figure anywhere else is a second
sum that can disagree with the money it sits next to. It is also why a rate row
publishes a figure per resource-second rather than one blended figure per
millisecond. A blend can only be reproduced from the placement table that
produced it, where a quantity times a rate can be recomputed from the row itself.

The billing tables answer in instants, never in dates. `func.date`, `date_trunc`
and anything else that reads the session `TimeZone` are out: nothing in this
repository sets that variable, so the day a cost landed in would be decided by
whichever connection wrote it. Period and validity boundaries are `timestamptz`
values a caller computed and passed. Cost is append-only. A written
`billing_ledger_segments` row is never updated or deleted, because a rate
published later must not reprice usage a customer has already been shown, and
that same row carries the attribution the cost is read back by, so no second
projection has to be kept in step with it.
