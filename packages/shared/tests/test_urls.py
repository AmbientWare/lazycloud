import pytest
from shared.urls import normalize_http_origin


@pytest.mark.parametrize(
    "value",
    [
        "",
        "control.example.test",
        "ftp://control.example.test",
        "https://",
        "https://user:secret@control.example.test",
        "https://control.example.test/api",
        "https://control.example.test?workspace=one",
        "https://control.example.test#fragment",
        "https://control.example.test:0",
        "https://control.example.test:99999",
    ],
)
def test_normalize_http_origin_rejects_non_origins(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_http_origin(value)
