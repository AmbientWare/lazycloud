import pytest
from shared.urls import normalize_http_origin, parse_http_address


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


def test_parse_http_address_takes_the_port_from_the_scheme() -> None:
    parsed = parse_http_address("https://bucket.s3.amazonaws.com", resource="object store")

    assert parsed.hostname == "bucket.s3.amazonaws.com"
    assert parsed.port is None
    assert parse_http_address("10.0.0.5:9000", resource="object store").port == 9000


@pytest.mark.parametrize(
    "value", ["ftp://bucket.s3.amazonaws.com", "https://", "https://host:port"]
)
def test_parse_http_address_rejects_non_http_targets(value: str) -> None:
    with pytest.raises(ValueError, match="object store address"):
        parse_http_address(value, resource="object store")
