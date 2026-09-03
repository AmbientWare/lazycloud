# Control package

Workspace, app, stub, deployment, resource, route, schedule, and concurrency
workflows: the control plane's decisions.

Services decide, database repositories persist, apps format. Use typed domain
errors and narrow event, context, and storage protocols rather than depending on
a broad service graph.

Every lookup, mutation, and registration carries tenant and owner scope. A query
that could return another workspace's row is a security defect, not a filtering
oversight, so scope belongs in the repository call rather than in the caller that
remembered to pass it.

Public payloads live in `shared.http`. Control and database records do not leak
into shared models.

An app is created without asking anybody. How many an account may have is not a
term of any plan, so there is nothing for a gate to read and a check there would
be one more transaction on the path that deploys.

A workspace is different, because a plan states how many an account gets.
`create_workspace` asks a `WorkspaceCreationAdmission` before anything is
written, and only when no workspace already carries the name.
`set_workspace` adopting an existing one is not a creation, and gating it would
lock an account out of workspaces it already holds. The service carries the
billing implementation as its default rather than waiting to be handed one,
since a limit only the API enforces is a limit every other entry point grants.
