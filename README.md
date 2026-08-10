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

## CLI Examples

```bash
lazycloud login --endpoint http://127.0.0.1:8000   # approve the code in the web app
lazycloud quickstart                               # write quickstart.py
lazycloud deploy quickstart.py:hello               # deploy the starter function
lazycloud run quickstart.py:hello 'LazyCloud'         # invoke it
lazycloud task list                                # find the Run id
lazycloud task result <run-id>
lazycloud task logs <run-id>
```

`lazycloud login` resolves its endpoint from `--endpoint <url>`, then
`LAZYCLOUD_ENDPOINT`, then the stored profile, then the packaged hosted default;
a local stack needs the flag or the variable. Without `--token` it starts a
device-code flow: the CLI prints a verification URL and confirmation code, you
sign in to the web dashboard with your username and password to approve it, and
the CLI stores the token it mints in its profile. That token belongs to the
approving account and reaches every workspace that account belongs to, so
`--workspace` selects which one the profile acts in. Pass `--token` for
non-interactive logins.
