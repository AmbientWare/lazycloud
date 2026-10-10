"""Host the callback receiver on LazyCloud when your own app is not ready yet.

Deploy with `uv run lazycloud deploy video_highlights.inbox:inbox` and put the
printed URL plus `/video-highlights` in settings.CALLBACK_URL.
"""

from lazycloud import App, Image

from video_highlights.receiver import SIGNING_KEY, api

inbox = App("video_highlights_inbox")

receiver = inbox.asgi(
    name="highlights-receiver",
    image=Image.from_uv("."),
    cpu=0.25,
    memory="256Mi",
    concurrent_requests=8,
    keep_warm_seconds=600,
    # Callbacks carry a signature, not a workspace token; the route checks it.
    authorized=False,
    secrets=[SIGNING_KEY],
)(api)
