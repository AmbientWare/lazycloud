# Download an example

Users get complete projects through the public CLI:

```bash
uv tool install lazycloud-client
lazycloud example list
lazycloud example download yolo-training
cd yolo-training
uv sync
uv run lazycloud login
uv run lazycloud run app:train_yolo
```

No repository access is required. Each project's README covers setup, expected
results, and cleanup. The [public guides](https://docs.lazycloud.dev/examples)
show the full workflows.

## Maintain the catalog

Canonical sources live under
[the SDK's example assets](../packages/lazycloud/src/lazycloud/_examples).
Each directory contains `example.json` and a `project/` directory.
The CLI reads these directories through Python package resources; it has no
per-example registry.

To add an example, create one directory with metadata:

```json
{
  "name": "my-example",
  "description": "Describe the result users get.",
  "python_version": "3.12",
  "dependencies": []
}
```

Put all source, assets, and a README under `project/`. Declare only local
dependencies in the metadata. Remote dependencies belong in the workload's
image. Use relative imports within a multi-file app package so it can run from
any downloaded directory.

The CLI generates `pyproject.toml`, `.python-version`, and `.gitignore`.
It pins `lazycloud-client` to the installed SDK version. Do not add generated
files, credentials, environments, or lockfiles to the asset directory.
Users generate and commit their own `uv.lock` after downloading.

Update an example in place. Remove its catalog directory to remove it from
future SDK releases. No CLI code change is needed. Update the corresponding
guide in the same change; existing user downloads are independent copies.

## Check a change

From the repository root, download the real project into a temporary directory:

```bash
uv run --group workspace lazycloud example download my-example --output /tmp/my-example
```

Verify its imports and documented commands from that directory with the current
client wheels installed. Build both an sdist and wheel for the client and verify
the catalog from the wheel built from that sdist; source-checkout success alone
does not prove that users receive the assets.

Example behavior tests live under
[the SDK owner tests](../packages/lazycloud/tests/examples).
Run the tests for the example you changed, plus relevant SDK checks. Clean up
temporary downloads and any resources a live run creates.
