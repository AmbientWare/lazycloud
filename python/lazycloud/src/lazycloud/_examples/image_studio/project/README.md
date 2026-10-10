# Image studio

A web app that turns prompts into images with FLUX.2 [klein] 4B. One CPU
deployment serves the Next.js page and the FastAPI routes behind it. Each job
runs as a GPU function call, the page follows its progress over a WebSocket,
and finished images stay in a gallery for seven days. Any image can become a
public link that expires.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m image_studio.configure
uv run lazycloud deploy image_studio.app:app
```

`configure` asks you to choose a studio key of 16 or more characters, stores it
as the `IMAGE_STUDIO_KEY` secret, and downloads about 16 GB of model weights
into the `image-studio-models` volume on a CPU container. Deploy prints the
`studio` URL. Open it, enter the key, and generate.

The `studio` URL is public, and the key is what guards the API and the GPU
behind it. Anyone with the key can spend GPU time, up to 16 queued jobs of at
most four images each.

The studio container stops two minutes after the last request, and the GPU
container five minutes after the last job. The cleanup schedule runs nightly at
04:00 UTC. Stop it with `uv run lazycloud deployment stop delete_expired_images`.

Clean up with `uv run lazycloud app delete image_studio`, then delete the
`image-studio-models` and `image-studio-gallery` volumes, the
`image-studio-progress` map, the `IMAGE_STUDIO_KEY` secret, and any shared
artifacts.

[Full guide](https://docs.lazycloud.dev/examples/image-studio)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your
code; use `uv sync --locked` in CI. Remote images install the `gpu` and
`weights` groups from the same lockfile, and the web image builds the page
from `web/package-lock.json` with a pinned Node.js release.
