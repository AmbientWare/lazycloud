# Public SDK

`lazycloud` is the backend-free user SDK and the public CLI: everything a user of
the platform imports or runs.

It may depend on `shared` and on nothing else in this repository: never apps,
backend domains, SQLAlchemy, Redis, FastAPI, schedulers, or workers. That
constraint is what makes it installable beside arbitrary user code, so it is a
boundary rather than a preference.

Keep the decorator and resource APIs ergonomic, low-level clients under
`lazycloud.clients`, bound high-level workflows under `lazycloud.session`, and
shared public commands under `lazycloud.cli`. Clients speak the current public
HTTP contracts; transport failures raise `HttpApiError`, and abstractions
translate those once into typed operation errors.

A project image's context and digest are exactly what its build reads: the
manifests plus the local dependency directories they name (uv lock sources and
workspace members, Poetry directory dependencies). The root project's own
sources never enter the context. Hashing the whole tree would rebuild the image
on every code edit; the code reaches the container through source sync instead.
A dependency path outside the project root is refused rather than uploaded.

Source sync reads `.lazycloudignore` with gitignore syntax and writes one with
the defaults when the sync root has none. Only the user-facing sync entry points
(`DeploymentClient` source upload and `ContainerWorkspaceSyncer`) write it;
`collect_source_files` and image project contexts never do, so building an
image does not drop files into a dependency directory. `BASELINE_IGNORE_PATTERNS`
applies on every sync on top of whatever the file says. A user who trims the file
to `data/` must not start shipping their virtualenv, `.git` or `.env` to the
container, and the baseline is the only place that promise is kept.

Use explicit client injection rather than ambient construction, and delete old
import paths instead of aliasing them. A public API is only cheap to keep honest
while it is still small.
