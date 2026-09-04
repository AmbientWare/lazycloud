# Database package

PostgreSQL, SQLAlchemy, and Alembic: the platform's durable state.

Use explicit relational tables and constraints. Put an invariant in the schema
wherever the schema can hold it: a rule enforced only in Python is enforced only
where someone remembered to call it. Repositories map and query; services decide.
Redis hot state stays out unless the history itself has to be durable.

There is a deployed installation holding data nobody can reconstruct, so the
schema moves by migration. Every change to a table adds a revision chained onto
the one before it, and `0001_initial` is frozen: a deployed database records the
revision it reached, and editing the file it points at makes that record a lie.
Local databases are still disposable and recreating one is ordinary work. Never
reset external, deployed, or production data.

The revisions are explicit DDL, never `create_all`. A migration generated from
the live metadata always agrees with it, which sounds like safety and is the
opposite: nothing could detect a model that changed without a revision to carry
a live database across. Explicit DDL is the fixed thing the metadata is compared
against, and
`test_a_model_changed_without_a_revision_is_caught_here` is that comparison. It
fails in a pull request rather than in a bootstrap job against production.

A PostgreSQL extension the schema needs is declared twice, with the metadata in
`tables/base.py` and again in the initial revision. The metadata installs them
through a `before_create` listener that fires for `create_all` and never for a
migration, so a schema built by migration would reach the first exclusion
constraint without the operator class it needs.

Two foreign keys carry `use_alter`, `apps.stub_id` and `containers.task_id`,
because apps and stubs point at each other and so do containers and tasks. A
schema with a cycle has no order that creates both tables with their keys
inline. The nullable pointer is the edge broken out in each case, and the
migration adds it once every table exists.

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
