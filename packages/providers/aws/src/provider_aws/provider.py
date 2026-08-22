from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, Self, TypeGuard, TypeVar, overload, runtime_checkable

from boto3.session import Session
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.app_identity import NAME
from shared.image_building.credentials import (
    EcrRegistryRef,
    parse_ecr_registry,
    registry_hosts_equal,
)


class AwsCredentialSource(StrEnum):
    Default = "default"
    Profile = "profile"
    Static = "static"


class AwsService(StrEnum):
    Ecr = "ecr"


class AwsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AwsProviderSettings(AwsModel):
    region: str = "us-east-1"
    profile: str | None = None
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    cluster_name: str = NAME
    ec2_ami: str = ""
    ec2_subnet_id: str | None = None
    gateway_url: str = ""
    ec2_root_volume_gib: int = Field(default=200, ge=1)

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        has_access_key = bool(self.access_key_id)
        has_secret_key = bool(self.secret_access_key)
        if has_access_key != has_secret_key:
            msg = "AWS static credentials require both access_key_id and secret_access_key"
            raise ValueError(msg)
        if self.profile and has_access_key:
            msg = "AWS profile and static credentials cannot be used together"
            raise ValueError(msg)
        return self


class AwsSessionOptions(AwsModel):
    region_name: str
    credential_source: AwsCredentialSource
    profile_name: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None

    def boto3_kwargs(self) -> dict[str, str]:
        kwargs = {"region_name": self.region_name}
        if self.profile_name is not None:
            kwargs["profile_name"] = self.profile_name
        if self.aws_access_key_id is not None:
            kwargs["aws_access_key_id"] = self.aws_access_key_id
        if self.aws_secret_access_key is not None:
            kwargs["aws_secret_access_key"] = self.aws_secret_access_key
        if self.aws_session_token is not None:
            kwargs["aws_session_token"] = self.aws_session_token
        return kwargs


class AwsClientOptions(AwsModel):
    service: AwsService
    session: AwsSessionOptions
    endpoint_url: str | None = None

    def boto3_kwargs(self) -> dict[str, str]:
        if self.endpoint_url is None:
            return {}
        return {"endpoint_url": self.endpoint_url}


class EcrAuthorizationPlan(AwsModel):
    registry: EcrRegistryRef
    region: str


class EcrAuthorization(AwsModel):
    registry: str
    username: str
    password: str
    expires_at: datetime | None = None


class _EcrAuthorizationData(AwsModel):
    authorization_token: str = Field(alias="authorizationToken")
    proxy_endpoint: str = Field(alias="proxyEndpoint")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")


class _EcrAuthorizationResponse(AwsModel):
    authorization_data: list[_EcrAuthorizationData] = Field(alias="authorizationData")


@runtime_checkable
class EcrAuthorizationClient(Protocol):
    def get_authorization_token(self, *, registryIds: list[str]) -> object: ...


_SessionT = TypeVar("_SessionT", covariant=True)
_ClientT = TypeVar("_ClientT", covariant=True)


class AwsBoto3Module(Protocol[_SessionT]):
    def Session(self, **kwargs: str) -> _SessionT: ...


class AwsClientSession(Protocol[_ClientT]):
    def client(self, service: str, **kwargs: str) -> _ClientT: ...


class _RuntimeClientFactory(Protocol):
    def client(self, service: str, **kwargs: str) -> object: ...


def _is_runtime_client_factory(value: object) -> TypeGuard[_RuntimeClientFactory]:
    return callable(getattr(value, "client", None))


@dataclass
class AwsProvider:
    settings: AwsProviderSettings = field(default_factory=AwsProviderSettings)

    def session_options(self) -> AwsSessionOptions:
        if self.settings.access_key_id and self.settings.secret_access_key:
            return AwsSessionOptions(
                region_name=self.settings.region,
                credential_source=AwsCredentialSource.Static,
                aws_access_key_id=self.settings.access_key_id,
                aws_secret_access_key=self.settings.secret_access_key,
                aws_session_token=self.settings.session_token,
            )
        if self.settings.profile:
            return AwsSessionOptions(
                region_name=self.settings.region,
                credential_source=AwsCredentialSource.Profile,
                profile_name=self.settings.profile,
            )
        return AwsSessionOptions(
            region_name=self.settings.region,
            credential_source=AwsCredentialSource.Default,
        )

    def client_options(self, service: AwsService) -> AwsClientOptions:
        return AwsClientOptions(
            service=service,
            session=self.session_options(),
            endpoint_url=self.settings.endpoint_url,
        )

    @overload
    def session(self) -> Session: ...

    @overload
    def session(self, *, boto3_module: AwsBoto3Module[_SessionT]) -> _SessionT: ...

    def session(
        self,
        *,
        boto3_module: AwsBoto3Module[_SessionT] | None = None,
    ) -> Session | _SessionT:
        options = self.session_options().boto3_kwargs()
        if boto3_module is not None:
            return boto3_module.Session(**options)
        session_options = self.session_options()
        return Session(
            region_name=session_options.region_name,
            profile_name=session_options.profile_name,
            aws_access_key_id=session_options.aws_access_key_id,
            aws_secret_access_key=session_options.aws_secret_access_key,
            aws_session_token=session_options.aws_session_token,
        )

    @overload
    def client(
        self,
        service: AwsService,
        *,
        session: AwsClientSession[_ClientT],
    ) -> _ClientT: ...

    @overload
    def client(
        self,
        service: Literal[AwsService.Ecr],
        *,
        session: None = None,
    ) -> EcrAuthorizationClient: ...

    def client(
        self,
        service: AwsService,
        *,
        session: AwsClientSession[_ClientT] | None = None,
    ) -> _ClientT | EcrAuthorizationClient:
        options = self.client_options(service)
        if session is not None:
            return session.client(options.service.value, **options.boto3_kwargs())
        source: object = self.session()
        if not _is_runtime_client_factory(source):
            raise RuntimeError("boto3 session lacks the client factory operation")
        candidate = source.client(options.service.value, **options.boto3_kwargs())
        if service is AwsService.Ecr and isinstance(candidate, EcrAuthorizationClient):
            return candidate
        raise RuntimeError(f"boto3 {service.value} client lacks required operations")

    def ecr_authorization_plan(self, registry_host: str) -> EcrAuthorizationPlan:
        registry = parse_ecr_registry(registry_host)
        if registry is None:
            msg = f"not an ECR registry host: {registry_host}"
            raise ValueError(msg)
        return EcrAuthorizationPlan(
            registry=registry,
            region=registry.region,
        )

    def ecr_authorization(
        self,
        registry_host: str,
        *,
        ecr_client: EcrAuthorizationClient | None = None,
    ) -> EcrAuthorization:
        plan = self.ecr_authorization_plan(registry_host)
        client = ecr_client or self.client(AwsService.Ecr)
        response = _EcrAuthorizationResponse.model_validate(
            client.get_authorization_token(registryIds=[plan.registry.account_id])
        )
        expected_registry = plan.registry.host.lower()
        entry = next(
            (
                item
                for item in response.authorization_data
                if registry_hosts_equal(item.proxy_endpoint, expected_registry)
            ),
            None,
        )
        if entry is None:
            raise RuntimeError(f"ECR did not return authorization for {plan.registry.host}")
        try:
            decoded = base64.b64decode(entry.authorization_token, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("ECR returned an invalid authorization token") from exc
        username, separator, password = decoded.partition(":")
        if not separator or not username or not password:
            raise RuntimeError("ECR authorization token is missing username/password material")
        return EcrAuthorization(
            registry=plan.registry.host,
            username=username,
            password=password,
            expires_at=entry.expires_at,
        )


__all__ = [
    "AwsClientOptions",
    "AwsCredentialSource",
    "AwsProvider",
    "AwsProviderSettings",
    "AwsService",
    "AwsSessionOptions",
    "EcrAuthorization",
    "EcrAuthorizationClient",
    "EcrAuthorizationPlan",
]
