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

Downloads need no repository access. Each README covers setup, results and
cleanup, and the [public guides](https://docs.lazycloud.dev/examples) walk
through the code.

## Maintain the catalog

Sources live in [the SDK's example assets](src/lazycloud/_examples), one
directory per example with `example.json` and `project/`. The CLI reads them as
package resources, so adding a directory adds an example.

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

Deleting a directory drops the example from the next SDK release. Change the
guide in the same commit as its project. Downloads already made are the
user's own copies.

## Check a change

From the repository root, download the real project into a temporary directory:

```bash
uv run --group workspace lazycloud example download my-example --output /tmp/my-example
```

Run its imports and guide commands from that directory. A source checkout can
hide missing assets, so check the catalog from a wheel built from the sdist.

Example tests live in [tests/examples](tests/examples). A live run leaves
resources behind; each guide's cleanup section removes them.
