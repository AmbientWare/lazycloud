from __future__ import annotations

from api.server.client_version import ClientVersionMiddleware
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from shared.client_version import RECOMMENDED_CLIENT_VERSION_HEADER
from starlette.responses import StreamingResponse


def test_version_advice_preserves_json_errors_and_streams() -> None:
    app = FastAPI()
    app.add_middleware(ClientVersionMiddleware, recommended_version=lambda: "1.2.3")

    @app.get("/data")
    def data() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/denied")
    def denied() -> None:
        raise HTTPException(status_code=403, detail="denied")

    @app.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(iter([b"first\n", b"second\n"]))

    with TestClient(app) as client:
        response = client.get("/data")
        assert response.json() == {"ok": True}
        assert response.headers[RECOMMENDED_CLIENT_VERSION_HEADER] == "1.2.3"
        response = client.get("/denied")
        assert response.status_code == 403
        assert response.json() == {"detail": "denied"}
        assert response.headers[RECOMMENDED_CLIENT_VERSION_HEADER] == "1.2.3"
        with client.stream("GET", "/stream") as response:
            assert response.headers[RECOMMENDED_CLIENT_VERSION_HEADER] == "1.2.3"
            assert list(response.iter_lines()) == ["first", "second"]
