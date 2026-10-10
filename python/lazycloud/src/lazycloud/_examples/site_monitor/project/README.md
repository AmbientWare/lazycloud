# Site monitor

Every 15 minutes a schedule checks each watched page in headless Chromium,
compares its readable text with the last check, and asks an OpenAI model
whether the change matters. Changes that matter post to Slack. An HTTP API
adds, lists and removes watched pages.

Run these commands from this downloaded project directory:

```bash
uv sync
uv run lazycloud login
uv run python -m site_monitor.configure
uv run lazycloud deploy site_monitor.app:app
```

`configure` asks for an OpenAI API key and a Slack incoming webhook URL and
stores them as workspace secrets. Deploy prints the `watches` API URL. Add a
page with a workspace access token:

```bash
curl --fail-with-body "<watches-url>/watches" \
  -H "Authorization: Bearer $LAZYCLOUD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/pricing", "focus": "price or plan changes"}'
```

The first check of a page stores a baseline, and later checks alert Slack when
the page changes in a way that matters. Run a check now with
`uv run lazycloud run site_monitor.app:sweep`.

Stop the schedule with `uv run lazycloud deployment stop sweep`. Delete the
app, the `site-monitor-watches` and `site-monitor-snapshots` maps, and the
two secrets when you no longer need them.

[Full guide](https://docs.lazycloud.dev/examples/site-monitor)

## Keep your project reproducible

`pyproject.toml` pins the SDK version that supplied this example. `uv sync`
creates the local environment and `uv.lock`. Commit the lockfile with your
code; use `uv sync --locked` in CI. Both remote images install from the same
lockfile, and the browser image adds the `check` group.
