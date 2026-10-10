# Video highlights

Give the pipeline the key of a video in your S3 bucket. ffmpeg extracts the
audio on a CPU, faster-whisper transcribes it on a GPU, an OpenAI model picks
chapters and highlights, and parallel CPU functions cut each highlight into a
clip and a thumbnail. The run returns a manifest with share links and, when
`CALLBACK_URL` is set, posts a signed callback to your app.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m video_highlights.configure
uv run lazycloud deploy video_highlights.pipeline:app
uv run lazycloud --json run video_highlights.pipeline:highlight_video talks/keynote.mp4
```

Set `BUCKET_NAME` and `BUCKET_REGION` in `video_highlights/settings.py`
first. `configure` asks for an access key that can read the bucket and an
OpenAI API key, and stores them as workspace secrets. Transcripts stay in the
`video-highlights-work` volume, so a second run of the same video skips the
GPU.

To try the callback, deploy the receiver with
`uv run lazycloud deploy video_highlights.inbox:inbox`, set
`CALLBACK_URL` to its URL plus `/video-highlights`, and deploy the pipeline
again. `uv run lazycloud logs --deployment highlights-receiver` shows each
verified notification.

Delete the `video_highlights` and `video_highlights_inbox` apps, the
`video-highlights-work` volume, the three secrets and the run artifacts when
you no longer need them.

[Full guide](https://docs.lazycloud.dev/examples/video-highlights)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your
code; use `uv sync --locked` in CI. Remote images install the `transcribe`
and `llm` groups from the same lockfile.
