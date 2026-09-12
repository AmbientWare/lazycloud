from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from shared.deployment_settings import MissingDeploymentSettingError
from shared.urls import normalize_http_origin

PUBLIC_HTTP_URL_VARIABLE = "LAZYCLOUD_GATEWAY_PUBLIC_HTTP_URL"
_TUNNEL_HOSTNAME_VARIABLE = "LAZYCLOUD_TUNNEL_HOSTNAME"
_TUNNEL_BOOTSTRAP_VARIABLE = "LAZYCLOUD_TUNNEL_GATEWAY_BOOTSTRAP_SECRET"
_TUNNEL_ISSUER_CERTIFICATE_VARIABLE = "LAZYCLOUD_TUNNEL_ISSUER_CERTIFICATE_FILE"
_TUNNEL_ISSUER_KEY_VARIABLE = "LAZYCLOUD_TUNNEL_ISSUER_PRIVATE_KEY_FILE"
_MISSING_ISSUER_PATH = Path("/__unset_lazycloud_tunnel_issuer__")


class GatewaySettings(BaseSettings):
    # Blank marks "nobody said", not an origin. This one is quoted back to
    # people and to other systems: it is the OAuth redirect GitHub matches, the
    # origin in the authorization template a customer applies to their own AWS
    # account, and the address a node enrols against. A localhost default would
    # be accepted everywhere and correct nowhere.
    public_http_url: str = Field(default="", validation_alias=PUBLIC_HTTP_URL_VARIABLE)

    model_config = SettingsConfigDict(
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("public_http_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        if not value.strip():
            return value
        return normalize_http_origin(value, field_name="gateway HTTP URL")

    @model_validator(mode="after")
    def require_public_http_url(self) -> GatewaySettings:
        if not self.public_http_url.strip():
            raise MissingDeploymentSettingError(
                PUBLIC_HTTP_URL_VARIABLE,
                purpose="the public origin this deployment is reached on",
            )
        return self

    @property
    def public_base_domain(self) -> str:
        """The host every generated resource hostname sits one label under.

        Derived from the public URL rather than configured beside it: two settings
        that must agree eventually disagree, and the one that loses sends traffic to
        a name nothing answers on.
        """

        return (urlparse(self.public_http_url).hostname or "").lower()


class TunnelGatewaySettings(BaseSettings):
    hostname: str = Field(default="", validation_alias=_TUNNEL_HOSTNAME_VARIABLE)
    gateway_bootstrap_secret: SecretStr = Field(
        default=SecretStr(""), validation_alias=_TUNNEL_BOOTSTRAP_VARIABLE, repr=False
    )

    model_config = SettingsConfigDict(
        extra="ignore", populate_by_name=True, hide_input_in_errors=True
    )

    @model_validator(mode="after")
    def require_gateway_identity(self) -> TunnelGatewaySettings:
        if not self.hostname:
            raise MissingDeploymentSettingError(
                _TUNNEL_HOSTNAME_VARIABLE, purpose="the hostname agents use for their TLS tunnel"
            )
        if len(self.hostname) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in self.hostname.split(".")
        ):
            raise ValueError("Tunnel hostname must be a canonical DNS name")
        secret = self.gateway_bootstrap_secret.get_secret_value()
        if not secret:
            raise MissingDeploymentSettingError(
                _TUNNEL_BOOTSTRAP_VARIABLE, purpose="the dedicated gateway bootstrap credential"
            )
        if len(secret) < 32:
            raise ValueError("Gateway bootstrap credential must contain at least 32 characters")
        return self

    @property
    def tunnel_address(self) -> str:
        return f"{self.hostname}:443"


class TunnelCertificateSettings(TunnelGatewaySettings):
    issuer_certificate_file: Path = Field(
        default=_MISSING_ISSUER_PATH, validation_alias=_TUNNEL_ISSUER_CERTIFICATE_VARIABLE
    )
    issuer_private_key_file: Path = Field(
        default=_MISSING_ISSUER_PATH, validation_alias=_TUNNEL_ISSUER_KEY_VARIABLE
    )

    @model_validator(mode="after")
    def require_issuer(self) -> TunnelCertificateSettings:
        for path, variable, purpose in (
            (
                self.issuer_certificate_file,
                _TUNNEL_ISSUER_CERTIFICATE_VARIABLE,
                "the tunnel CA certificate",
            ),
            (
                self.issuer_private_key_file,
                _TUNNEL_ISSUER_KEY_VARIABLE,
                "the protected tunnel CA signing key",
            ),
        ):
            if path == _MISSING_ISSUER_PATH:
                raise MissingDeploymentSettingError(variable, purpose=purpose)
            if not path.is_absolute():
                raise ValueError("Tunnel issuer files require absolute paths")
        return self


__all__ = [
    "PUBLIC_HTTP_URL_VARIABLE",
    "GatewaySettings",
    "TunnelCertificateSettings",
    "TunnelGatewaySettings",
]
