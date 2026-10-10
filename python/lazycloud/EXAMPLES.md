# Download an example

Users get complete projects through the public CLI:

```bash
uv tool install lazycloud-client
lazycloud example list
lazycloud example download image-studio
cd image-studio
uv sync
uv run lazycloud login --token <token>
uv run lazycloud deploy image_studio.app:app
```

No repository access is required. Each project's README covers setup, expected
results, and cleanup. The [public guides](https://docs.lazycloud.dev/examples)
show the full workflows.

## Maintain the catalog

Canonical sources live under
[the SDK's example assets](src/lazycloud/_examples).
Each directory contains `example.json` and a `project/` directory.
The CLI reads these directories through Python package resources; it has no
per-example registry.

To add an example, create one directory with metadata:

```json
{
  "name": "my-example",
  "description": "Describe the result users get."
}
```

Put all source, assets, a README, `pyproject.toml` and `.python-version`
under `project/`. List `"lazycloud-client"` once, unpinned, in the `dev`
dependency group; the CLI pins it to the installed SDK version on download,
and `Image.from_uv` leaves the group out of remote images. Put each
workload's remote dependencies in its own dependency group and build its
image with `Image.from_uv(".", groups=[...])`. Do not add credentials,
environments or lockfiles to the asset directory. Users generate and commit
their own `uv.lock` after downloading.

Guides quote project files in code blocks titled with the file's path, such
as ```` ```python image_studio/app.py ````. `tests/examples/test_example_docs.py`
fails when a quoted block differs from the shipped file.

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
[the SDK owner tests](tests/examples).
Run the tests for the example you changed, plus relevant SDK checks. Clean up
temporary downloads and any resources a live run creates.
