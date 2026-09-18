# Database package

PostgreSQL, SQLAlchemy, and Alembic: the platform's durable state.

Use explicit relational tables and constraints. Put an invariant in the schema
wherever the schema can hold it: a rule enforced only in Python is enforced only
where someone remembered to call it. Repositories map and query; services decide.
Redis hot state stays out unless the history itself has to be durable.

`0001_relational_baseline` is deployed and frozen. Schema changes require forward
migrations; editing a deployed revision invalidates the database's history.
The completed owner-authorized reset in PR #295 does not authorize another
production reset. Local databases remain disposable.

The revisions are explicit DDL, never `create_all`. A migration generated from
the live metadata always agrees with it, which sounds like safety and is the
opposite: nothing could detect a model that changed without a revision to carry
a live database across. Explicit DDL is the fixed thing the metadata is compared
against, and
`test_postgresql_baseline_matches_metadata_constraints_and_indexes` is that
comparison. It fails in a pull request rather than in a bootstrap job against
production.

A PostgreSQL extension the schema needs is declared twice, with the metadata in
`tables/base.py` and again in the initial revision. The metadata installs them
through a `before_create` listener that fires for `create_all` and never for a
migration, so a schema built by migration would reach the first exclusion
constraint without the operator class it needs.

Foreign keys that close dependency cycles carry `use_alter`. These include the
app's current stub, the stub's deployment, the container's task, and the
workspace's primary token and concurrency limit. The baseline adds those
nullable pointers after creating every table.

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

A machine's `name` is unique per `owner_user_id` among rows that are not
deleted, through a partial unique index rather than a Python check, because the
name is claimed the moment a join command is minted and two mints can race. A
deleted row keeps its name as history and stops holding it. `machine_workspaces`
lists the workspaces a joined machine serves and cascades with both sides.
