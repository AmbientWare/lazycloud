# LazyCloud

LazyCloud is a Python-first toolkit for defining, registering, and running
workloads with local development semantics that mirror remote execution
workflows.

```python
from lazycloud import App, Image

app = App("quickstart")
image = Image(python_version="3.12").add_python_packages(["httpx"])


@app.function(image=image, cpu=1.0, memory="512Mi")
def hello(name: str = "world") -> str:
    return f"hello {name}"


if __name__ == "__main__":
    print(hello.local("lazycloud"))
```

## Install From Source

```bash
uv tool install ./packages/lazycloud
lazycloud --help
```

Run the install command from the repository root. Installing the `lazycloud`
distribution provides both the `import lazycloud` Python SDK and the `lazycloud`
command. A registry install command will be documented after the distribution
is published for the beta.

Local working files are stored under `~/.lazycloud` by default. Durable application
state is stored in PostgreSQL and hot coordination state is stored in Redis.
Set `LAZYCLOUD_HOME=/path/to/state` to isolate local filesystem resources.

## Development Validation

Validate the owner and production boundary changed by the work. During normal
iteration, run Ruff and formatting on the changed files, type-check the affected
owner, and run that owner's tests:

```bash
uv run --group dev ruff check packages/scheduler/src packages/scheduler/tests
uv run --group dev ruff format --check packages/scheduler/src packages/scheduler/tests
uv run --group dev basedpyright packages/scheduler
uv run --group dev pytest -q packages/scheduler/tests
```

Run `uv lock --check` only when dependency or workspace metadata changes.
Pull-request CI derives this scope from the changed files. Web and Compose
validation run only when their owning paths change. Live Docker, browser, and
provider workflows are explicit feature or release acceptance, not blanket
development checks. Release validation is defined by the release
task and the production boundaries changed since the previous release.

## Local CLI examples

Run `bash deploy/setup-local-env.sh` once per clone, then fill the private `.env`
in the main checkout using development credentials. Existing and future worktrees
link to that file; an existing worktree-specific `.env` is preserved. PostgreSQL
and Redis stay local, Stripe uses test mode, and GitHub uses the development App.
See [local deployment](deploy/README.md#local-environment) for startup.

```bash
uv run lazycloud-admin login --profile local
uv run lazycloud-admin quickstart
uv run lazycloud-admin deploy quickstart.py:hello
uv run lazycloud-admin run quickstart.py:hello 'LazyCloud'
uv run lazycloud-admin task list
uv run lazycloud-admin task result <run-id>
uv run lazycloud-admin task logs <run-id>
```

`lazycloud-admin` loads the repository's `.env`. The example configuration points
it at the Compose control plane, stores its profile under `~/.lazycloud/local`, and
uses the administrator token that bootstrapped the stack. The command validates
that token before saving the `local` profile.

The public `lazycloud` command does not read `.env` from the current directory.
It uses exported `LAZYCLOUD_*` variables and its active profile, so entering a
repository cannot silently redirect commands to another control plane. Export
`LAZYCLOUD_HOME`, `LAZYCLOUD_ENDPOINT`, and `LAZYCLOUD_TOKEN` when you need the
public CLI to use this local stack.
