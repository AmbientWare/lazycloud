import pytest
from gateway.settings import GatewaySettings
from shared.deployment_settings import MissingDeploymentSettingError


def test_gateway_settings_keep_public_and_runtime_origins_independent() -> None:
    configured = GatewaySettings(
        public_http_url="https://control.example.com/",
        runtime_callback_http_url="http://control-plane:9000/",
    )

    assert configured.public_http_url == "https://control.example.com"
    assert configured.runtime_callback_http_url == "http://control-plane:9000"


@pytest.mark.parametrize(
    "value",
    [
        "control.example.com",
        "ftp://control.example.com",
        "https://",
        "https://user:secret@control.example.com",
        "https://control.example.com/api",
        "https://control.example.com?workspace=one",
        "https://control.example.com#fragment",
        "https://control.example.com:0",
        "https://control.example.com:99999",
    ],
)
def test_gateway_settings_reject_non_origin_urls(value: str) -> None:
    with pytest.raises(ValueError):
        GatewaySettings(
            public_http_url=value,
            runtime_callback_http_url="http://control-plane:9000",
        )


def test_gateway_settings_name_the_variable_when_no_public_origin_is_set() -> None:
    """A blank public origin is an unconfigured deployment, not a malformed URL.

    It is the OAuth redirect, the origin in a customer's authorization template,
    and the address a node enrols against, so the process refuses to start rather
    than hand out one nobody chose.
    """

    with pytest.raises(MissingDeploymentSettingError) as raised:
        GatewaySettings(
            public_http_url="",
            runtime_callback_http_url="http://control-plane:9000",
        )

    assert raised.value.variable == "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"
