# Control Package

Workspace, app, stub, deployment, resource, route, cron, and concurrency
workflows—the control plane's decisions.

Services decide, database repositories persist, apps format. Use typed domain
errors and narrow event, context, and storage protocols rather than depending on
a broad service graph.

Every lookup, mutation, and registration carries tenant and owner scope. A query
that could return another workspace's row is a security defect, not a filtering
oversight, so scope belongs in the repository call rather than in the caller that
remembered to pass it.

Public payloads live in `shared.http`. Control and database records do not leak
into shared models.
