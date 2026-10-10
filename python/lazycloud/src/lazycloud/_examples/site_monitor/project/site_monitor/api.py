"""HTTP routes that add, list and remove watched pages."""

from fastapi import FastAPI, HTTPException, Response
from lazycloud import Map

from site_monitor.models import MAX_WATCHES, Watch, WatchRequest


def create_api(watches: Map, snapshots: Map) -> FastAPI:
    api = FastAPI(title="Site monitor")

    @api.get("/watches")
    def list_watches() -> list[Watch]:
        found = (watches.get(key) for key in watches)
        return sorted(
            (Watch.model_validate(value) for value in found if value is not None),
            key=lambda watch: str(watch.url),
        )

    @api.post("/watches")
    def add_watch(request: WatchRequest) -> Watch:
        watch = Watch.from_request(request)
        if watch.id not in watches and len(watches) >= MAX_WATCHES:
            raise HTTPException(409, f"already watching {MAX_WATCHES} pages")
        watches.set(watch.id, watch.model_dump(mode="json"), ttl=0)
        return watch

    @api.delete("/watches/{watch_id}", status_code=204)
    def remove_watch(watch_id: str) -> Response:
        try:
            del watches[watch_id]
        except KeyError:
            raise HTTPException(404, f"no watch {watch_id}") from None
        snapshots.pop(watch_id, None)
        return Response(status_code=204)

    return api
