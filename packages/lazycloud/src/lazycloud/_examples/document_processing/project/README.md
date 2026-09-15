# Upload PDFs and images and extract their text with OCR.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m document_processing.configure
uv run lazycloud deploy document_processing.app:app
```

Run the configuration module once per workspace. It creates a random signing
secret and refuses to overwrite an existing one. On redeployment, skip that step.

Open the printed `document-api` URL. This example accepts public uploads; use
test documents and stop the service when finished. Both workloads share a
declared volume, created on first use. The browser's Delete result action removes
completed results. Rotating the signing secret invalidates outstanding job tokens.

Delete the `document-api` and `ocr-worker` deployments to stop the app.
Delete its volume and secret separately after saving any results you need.

[Full guide](https://docs.lazycloud.dev/examples/document-processing-asgi)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your code;
use `uv sync --locked` in CI. Keep credentials out of source control.
Remote GPU and system dependencies are defined in the workload's image.

Downloading and installing the project creates no cloud resources. Running or
deploying workloads can incur charges. Inspect CLI logs when a run fails.
