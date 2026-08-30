from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
)
from shared.custom_domains import (
    CustomDomainDnsMode,
    CustomDomainErrorCode,
    CustomDomainPhase,
    DnsRecord,
    ProviderCustomHostname,
)
from shared.errors import InvalidInputError, UpstreamUnavailableError


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Envelope(_Model):
    data: JsonValue = None
    success: bool
    error: bool = False
    message: str = ""
    status: int


class PangolinSite(_Model):
    site_id: int = Field(alias="siteId", gt=0)
    nice_id: str = Field(default="", alias="niceId")
    name: str
    online: bool = False
    address: str | None = None
    newt_id: str | None = Field(default=None, alias="newtId")


class PangolinCreatedSite(_Model):
    site_id: int = Field(alias="siteId", gt=0)
    nice_id: str = Field(default="", alias="niceId")
    name: str
    online: bool = False
    address: str | None = None
    newt_id: str = Field(alias="newtId", min_length=1)
    secret: SecretStr


class PangolinPrivateResource(_Model):
    site_resource_id: int = Field(alias="siteResourceId", gt=0)
    name: str
    alias_address: str = Field(alias="aliasAddress", min_length=1)
    mode: Literal["host", "cidr", "http", "ssh", "inference"]
    destination: str | None = None
    tcp_port_range: str = Field(alias="tcpPortRangeString")
    udp_port_range: str = Field(alias="udpPortRangeString")
    site_ids: tuple[int, ...] = Field(default=(), alias="siteIds")


class _PrivateResourceListData(_Model):
    site_resources: tuple[PangolinPrivateResource, ...] = Field(default=(), alias="siteResources")


class PangolinPrivateResourceClient(_Model):
    client_id: int = Field(alias="clientId", gt=0)


class _PrivateResourceClientListData(_Model):
    clients: tuple[PangolinPrivateResourceClient, ...] = ()


class _SiteListData(_Model):
    sites: tuple[PangolinSite, ...] = ()


class PangolinClientRecord(_Model):
    client_id: int = Field(alias="clientId", gt=0)
    nice_id: str = Field(default="", alias="niceId")
    name: str
    olm_id: str | None = Field(default=None, alias="olmId")


class _ClientListData(_Model):
    clients: tuple[PangolinClientRecord, ...] = ()


class PangolinClientDefaults(_Model):
    olm_id: str = Field(alias="olmId", min_length=1)
    olm_secret: SecretStr = Field(alias="olmSecret")
    subnet: str = Field(min_length=1)


class PangolinCreatedClient(_Model):
    client_id: int = Field(alias="clientId", gt=0)
    nice_id: str = Field(default="", alias="niceId")
    name: str
    olm_id: str = Field(alias="olmId", min_length=1)
    secret: SecretStr


class PangolinDomain(_Model):
    domain_id: str = Field(alias="domainId", min_length=1)
    base_domain: str = Field(alias="baseDomain", min_length=1)
    type: Literal["ns", "cname", "wildcard"] | None = None
    verified: bool = False
    failed: bool = False
    config_managed: bool = Field(default=False, alias="configManaged")
    error_message: str | None = Field(default=None, alias="errorMessage")


class _DomainListData(_Model):
    domains: tuple[PangolinDomain, ...] = ()


class PangolinDnsRecord(_Model):
    record_type: str = Field(alias="recordType", min_length=1)
    base_domain: str = Field(alias="baseDomain", min_length=1)
    value: str = Field(min_length=1)
    verified: bool = False


class _CreateDomainData(_Model):
    domain_id: str = Field(alias="domainId", min_length=1)


class PangolinResource(_Model):
    resource_id: int = Field(alias="resourceId", gt=0)
    domain_id: str | None = Field(alias="domainId")
    name: str
    subdomain: str | None = None
    full_domain: str | None = Field(default=None, alias="fullDomain")
    http: bool
    protocol: Literal["tcp", "udp"]
    sso: bool
    enabled: bool


class _ResourceListData(_Model):
    resources: tuple[PangolinResource, ...] = ()


class PangolinTarget(_Model):
    target_id: int = Field(alias="targetId", gt=0)
    resource_id: int = Field(alias="resourceId", gt=0)
    site_id: int = Field(alias="siteId", gt=0)
    ip: str
    port: int = Field(gt=0, le=65535)
    method: str | None = None
    enabled: bool = True
    health_check_enabled: bool = Field(default=False, alias="hcEnabled")
    health_check_path: str | None = Field(default=None, alias="hcPath")
    health_check_scheme: str | None = Field(default=None, alias="hcScheme")
    health_check_mode: str | None = Field(default=None, alias="hcMode")
    health_check_hostname: str | None = Field(default=None, alias="hcHostname")
    health_check_port: int | None = Field(default=None, alias="hcPort")
    health_check_interval: int | None = Field(default=None, alias="hcInterval")
    health_check_unhealthy_interval: int | None = Field(
        default=None,
        alias="hcUnhealthyInterval",
    )
    health_check_timeout: int | None = Field(default=None, alias="hcTimeout")
    health_check_follow_redirects: bool | None = Field(
        default=None,
        alias="hcFollowRedirects",
    )
    health_check_method: str | None = Field(default=None, alias="hcMethod")
    health_check_status: int | None = Field(default=None, alias="hcStatus")
    health_check_healthy_threshold: int | None = Field(
        default=None,
        alias="hcHealthyThreshold",
    )
    health_check_unhealthy_threshold: int | None = Field(
        default=None,
        alias="hcUnhealthyThreshold",
    )


class _TargetListData(_Model):
    targets: tuple[PangolinTarget, ...] = ()


class PangolinApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, retryable: bool) -> None:
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


_PLATFORM_RESOURCE_NAME = "lazycloud-platform-public"
_TARGET_HEALTH: dict[str, JsonValue] = {
    "hcEnabled": True,
    "hcPath": "/health",
    "hcScheme": "http",
    "hcMode": "http",
    "hcInterval": 10,
    "hcUnhealthyInterval": 5,
    "hcTimeout": 3,
    "hcFollowRedirects": False,
    "hcMethod": "GET",
    "hcStatus": 200,
    "hcHealthyThreshold": 2,
    "hcUnhealthyThreshold": 3,
}


@dataclass(frozen=True, slots=True)
class PangolinClient:
    api_url: str
    api_key: SecretStr
    organization_id: str
    endpoint: str
    platform_identity_prefix: str = "lazycloud-platform"
    platform_site_ids: tuple[int, ...] = ()
    platform_client_record_ids: tuple[int, ...] = ()
    platform_target_host: str = ""
    platform_target_port: int = 9000
    timeout_seconds: float = 30.0
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def create_site(self, *, name: str) -> PangolinCreatedSite:
        data = self._request(
            "PUT",
            f"/org/{quote(self.organization_id, safe='')}/site",
            json={"name": name, "type": "newt"},
            allowed_status_codes=(200, 201),
        )
        return self._parse(data, PangolinCreatedSite, "site creation")

    def find_site_by_name(self, name: str) -> PangolinSite | None:
        candidates = tuple(site for site in self._sites(query=name) if site.name == name)
        if len(candidates) > 1:
            raise InvalidInputError(f"Pangolin has duplicate site identity for {name}")
        return self.get_site(candidates[0].site_id) if candidates else None

    def find_site(self, *, name: str, connector_id: str) -> PangolinSite | None:
        candidates = tuple(site for site in self._sites(query=name) if site.name == name)
        matches: list[PangolinSite] = []
        for candidate in candidates:
            site = self.get_site(candidate.site_id)
            if site is not None and site.newt_id == connector_id:
                matches.append(site)
        if len(matches) > 1:
            raise InvalidInputError(f"Pangolin has duplicate site identity for {name}")
        return matches[0] if matches else None

    def create_client(self, *, name: str) -> PangolinCreatedClient:
        defaults = self._parse(
            self._request(
                "GET",
                f"/org/{quote(self.organization_id, safe='')}/pick-client-defaults",
            ),
            PangolinClientDefaults,
            "machine-client defaults",
        )
        data = self._request(
            "PUT",
            f"/org/{quote(self.organization_id, safe='')}/client",
            json={
                "name": name,
                "olmId": defaults.olm_id,
                "secret": defaults.olm_secret.get_secret_value(),
                "subnet": defaults.subnet,
                "type": "olm",
            },
            allowed_status_codes=(200, 201),
        )
        created = self._parse(data, PangolinClientRecord, "machine-client creation")
        if created.olm_id != defaults.olm_id:
            raise PangolinApiError(
                "Pangolin created a machine client with an unexpected identity",
                status_code=200,
                retryable=False,
            )
        return PangolinCreatedClient(
            clientId=created.client_id,
            niceId=created.nice_id,
            name=created.name,
            olmId=defaults.olm_id,
            secret=defaults.olm_secret,
        )

    def find_client_by_name(self, name: str) -> PangolinClientRecord | None:
        candidates = tuple(client for client in self._clients(query=name) if client.name == name)
        if len(candidates) > 1:
            raise InvalidInputError(f"Pangolin has duplicate machine-client identity for {name}")
        return self.get_client(candidates[0].client_id) if candidates else None

    def get_client(self, client_id: int) -> PangolinClientRecord | None:
        data = self._request(
            "GET",
            f"/client/{client_id}",
            allowed_status_codes=(200, 404),
            missing_is_none=True,
        )
        return (
            None
            if data is None
            else self._parse(data, PangolinClientRecord, "machine-client lookup")
        )

    def delete_client(self, client_id: int) -> None:
        self._request(
            "DELETE",
            f"/client/{client_id}",
            allowed_status_codes=(200, 204, 404),
        )

    def ensure_agent_private_resource(
        self,
        *,
        site_id: int,
        destination_port: int,
    ) -> PangolinPrivateResource:
        platform_client_record_ids = self._platform_client_record_ids()
        name = f"lazycloud-site-{site_id}-route-proxy"
        existing = tuple(
            resource
            for resource in self._private_resources(site_id=site_id, query=name)
            if resource.name == name
        )
        if len(existing) > 1:
            raise InvalidInputError(f"Pangolin site {site_id} has duplicate LazyCloud resources")
        if existing:
            resource = existing[0]
            self._validate_agent_private_resource(
                resource,
                site_id=site_id,
                destination_port=destination_port,
                platform_client_record_ids=platform_client_record_ids,
            )
            return resource
        data = self._request(
            "PUT",
            f"/org/{quote(self.organization_id, safe='')}/private-resource",
            json={
                "name": name,
                "mode": "host",
                "siteIds": [site_id],
                "destination": "127.0.0.1",
                "userIds": [],
                "roleIds": [],
                "clientIds": list(platform_client_record_ids),
                "tcpPortRangeString": str(destination_port),
                "udpPortRangeString": "",
                "disableIcmp": True,
            },
            allowed_status_codes=(200, 201),
        )
        return self._parse(data, PangolinPrivateResource, "private resource creation")

    def _private_resources(
        self,
        *,
        site_id: int | None,
        query: str,
    ) -> tuple[PangolinPrivateResource, ...]:
        params = {
            "pageSize": "100",
            "page": "1",
            "query": query,
        }
        if site_id is not None:
            params["siteId"] = str(site_id)
        data = self._request(
            "GET",
            f"/org/{quote(self.organization_id, safe='')}/private-resources",
            params=params,
        )
        parsed = self._parse(data, _PrivateResourceListData, "private resource listing")
        return parsed.site_resources

    def synchronize_platform_client_access(self, client_ids: tuple[int, ...]) -> None:
        client_ids = tuple(sorted(client_ids))
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/org/{quote(self.organization_id, safe='')}/private-resources",
                params={
                    "pageSize": "100",
                    "page": str(page),
                    "query": "lazycloud-site-",
                },
            )
            resources = self._parse(
                data,
                _PrivateResourceListData,
                "private resource listing",
            ).site_resources
            for resource in resources:
                if not _is_agent_private_resource(resource.name):
                    continue
                current = self._private_resource_client_ids(resource.site_resource_id)
                if current == client_ids:
                    continue
                self._request(
                    "POST",
                    f"/private-resource/{resource.site_resource_id}/clients",
                    json={"clientIds": list(client_ids)},
                    allowed_status_codes=(200, 201),
                )
            if len(resources) < 100:
                return
            page += 1

    def synchronize_platform_site_targets(self, site_ids: tuple[int, ...]) -> None:
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/org/{quote(self.organization_id, safe='')}/public-resources",
                params={"pageSize": "100", "page": str(page)},
            )
            resources = self._parse(data, _ResourceListData, "resource listing").resources
            for resource in resources:
                if resource.domain_id is None or resource.name not in {
                    _resource_name(resource.domain_id, None),
                    _resource_name(resource.domain_id, "*"),
                }:
                    continue
                resource = self._ensure_public_resource_passthrough(resource)
                self._ensure_targets(resource, platform_site_ids=site_ids)
            if len(resources) < 100:
                return
            page += 1

    def ensure_platform_public_resource(
        self,
        *,
        hostname: str,
        site_ids: tuple[int, ...],
    ) -> PangolinResource:
        self._require_platform_target()
        normalized = hostname.strip().lower().rstrip(".")
        if not normalized:
            raise InvalidInputError("Pangolin platform public hostname is required")
        domains = tuple(
            domain
            for domain in self._domains()
            if domain.verified
            and (normalized == domain.base_domain or normalized.endswith(f".{domain.base_domain}"))
        )
        if not domains:
            raise InvalidInputError(f"Pangolin has no verified domain covering {normalized}")
        domain = max(domains, key=lambda item: len(item.base_domain))
        subdomain = (
            None
            if normalized == domain.base_domain
            else normalized[: -(len(domain.base_domain) + 1)]
        )
        resources = tuple(
            resource
            for resource in self._domain_resources(domain.domain_id)
            if resource.name == _PLATFORM_RESOURCE_NAME
        )
        if len(resources) > 1:
            raise InvalidInputError("Pangolin has duplicate LazyCloud platform resources")
        if resources:
            resource = resources[0]
            if (
                resource.full_domain != normalized
                or not resource.http
                or resource.protocol != "tcp"
            ):
                raise InvalidInputError(
                    "Pangolin platform resource does not match the configured hostname"
                )
        else:
            resource = self._create_resource(
                domain_id=domain.domain_id,
                name=_PLATFORM_RESOURCE_NAME,
                subdomain=subdomain,
            )
        resource = self._ensure_public_resource_passthrough(resource)
        self._ensure_targets(resource, platform_site_ids=site_ids)
        return resource

    def _validate_agent_private_resource(
        self,
        resource: PangolinPrivateResource,
        *,
        site_id: int,
        destination_port: int,
        platform_client_record_ids: tuple[int, ...],
    ) -> None:
        if (
            resource.mode != "host"
            or resource.destination != "127.0.0.1"
            or resource.tcp_port_range != str(destination_port)
            or resource.udp_port_range
            or resource.site_ids != (site_id,)
        ):
            raise InvalidInputError(
                f"Pangolin resource {resource.site_resource_id} no longer matches "
                "its LazyCloud route"
            )
        if (
            self._private_resource_client_ids(resource.site_resource_id)
            != platform_client_record_ids
        ):
            raise InvalidInputError(
                f"Pangolin resource {resource.site_resource_id} has unexpected machine access"
            )

    def _private_resource_client_ids(self, resource_id: int) -> tuple[int, ...]:
        data = self._request(
            "GET",
            f"/private-resource/{resource_id}/clients",
        )
        clients = self._parse(
            data,
            _PrivateResourceClientListData,
            "private resource client listing",
        ).clients
        return tuple(sorted(client.client_id for client in clients))

    def get_site(self, site_id: int) -> PangolinSite | None:
        data = self._request(
            "GET",
            f"/site/{site_id}",
            allowed_status_codes=(200, 404),
            missing_is_none=True,
        )
        return None if data is None else self._parse(data, PangolinSite, "site lookup")

    def delete_site(self, site_id: int) -> None:
        resource_name = f"lazycloud-site-{site_id}-route-proxy"
        for resource in self._private_resources(site_id=None, query=resource_name):
            if resource.name == resource_name:
                self.delete_private_resource(resource.site_resource_id)
        self._request(
            "DELETE",
            f"/site/{site_id}",
            allowed_status_codes=(200, 204, 404),
        )

    def delete_private_resource(self, resource_id: int) -> None:
        self._request(
            "DELETE",
            f"/private-resource/{resource_id}",
            allowed_status_codes=(200, 204, 404),
        )

    def create_hostname(
        self,
        hostname: str,
        *,
        dns_mode: CustomDomainDnsMode,
    ) -> ProviderCustomHostname:
        self._require_platform_target()
        domain_type = "cname" if dns_mode is CustomDomainDnsMode.Cname else "ns"
        data = self._request(
            "PUT",
            f"/org/{quote(self.organization_id, safe='')}/domain",
            json={"type": domain_type, "baseDomain": hostname},
            allowed_status_codes=(200, 201),
        )
        created = self._parse(data, _CreateDomainData, "domain creation")
        records = self._required_dns_records(created.domain_id)
        return ProviderCustomHostname(
            provider_hostname_id=_provider_hostname_id(dns_mode, created.domain_id),
            phase=CustomDomainPhase.AwaitingVerification,
            required_records=records,
        )

    def get_hostname(self, provider_hostname_id: str) -> ProviderCustomHostname | None:
        dns_mode, domain_id = _parse_provider_hostname_id(provider_hostname_id)
        path = self._domain_path(domain_id)
        data = self._request(
            "GET",
            path,
            allowed_status_codes=(200, 404),
            missing_is_none=True,
        )
        if data is None:
            return None
        domain = self._parse(data, PangolinDomain, "domain lookup")
        records = self._dns_records(domain_id)
        if domain.verified:
            self._ensure_public_resources(domain, dns_mode=dns_mode)
            phase = CustomDomainPhase.Ready
            error_code = None
        elif domain.failed:
            phase = CustomDomainPhase.ActionRequired
            error_code = CustomDomainErrorCode.CertificateFailed
        elif any(record.verified for record in records):
            phase = CustomDomainPhase.Validating
            error_code = None
        else:
            phase = CustomDomainPhase.AwaitingVerification
            error_code = None
        required = tuple(
            DnsRecord(type=record.record_type, name=record.base_domain, value=record.value)
            for record in records
            if not record.verified
        )
        return ProviderCustomHostname(
            provider_hostname_id=provider_hostname_id,
            phase=phase,
            required_records=required,
            error_code=error_code,
            error_message=(domain.error_message or "")[:512] or None,
        )

    def delete_hostname(self, provider_hostname_id: str) -> None:
        _, domain_id = _parse_provider_hostname_id(provider_hostname_id)
        for resource in self._domain_resources(domain_id):
            if resource.name.startswith(_resource_name_prefix(domain_id)):
                self._request(
                    "DELETE",
                    f"/public-resource/{resource.resource_id}",
                    allowed_status_codes=(200, 204, 404),
                )
        self._request(
            "DELETE",
            self._domain_path(domain_id),
            allowed_status_codes=(200, 204, 404),
        )

    def _ensure_public_resources(
        self,
        domain: PangolinDomain,
        *,
        dns_mode: CustomDomainDnsMode,
    ) -> None:
        self._require_platform_target()
        existing = self._domain_resources(domain.domain_id)
        desired_subdomains: tuple[str | None, ...] = (
            (None,) if dns_mode is CustomDomainDnsMode.Cname else (None, "*")
        )
        for subdomain in desired_subdomains:
            expected_name = _resource_name(domain.domain_id, subdomain)
            resource = next(
                (item for item in existing if item.name == expected_name),
                None,
            )
            if resource is None:
                resource = self._create_resource(
                    domain_id=domain.domain_id,
                    name=expected_name,
                    subdomain=subdomain,
                )
            expected_full_domain = (
                domain.base_domain if subdomain is None else f"{subdomain}.{domain.base_domain}"
            )
            if (
                resource.domain_id != domain.domain_id
                or resource.full_domain != expected_full_domain
                or not resource.http
                or resource.protocol != "tcp"
            ):
                raise InvalidInputError(
                    f"Pangolin resource {resource.resource_id} no longer matches "
                    "its LazyCloud hostname"
                )
            resource = self._ensure_public_resource_passthrough(resource)
            self._ensure_targets(resource)

    def _domain_resources(self, domain_id: str) -> tuple[PangolinResource, ...]:
        resources: list[PangolinResource] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/org/{quote(self.organization_id, safe='')}/public-resources",
                params={"pageSize": "100", "page": str(page)},
            )
            parsed = self._parse(data, _ResourceListData, "resource listing")
            resources.extend(
                resource for resource in parsed.resources if resource.domain_id == domain_id
            )
            if len(parsed.resources) < 100:
                return tuple(resources)
            page += 1

    def _create_resource(
        self,
        *,
        domain_id: str,
        name: str,
        subdomain: str | None,
    ) -> PangolinResource:
        data = self._request(
            "PUT",
            f"/org/{quote(self.organization_id, safe='')}/public-resource",
            json={
                "name": name,
                "subdomain": subdomain,
                "domainId": domain_id,
                "mode": "http",
            },
            allowed_status_codes=(200, 201),
        )
        return self._parse(data, PangolinResource, "resource creation")

    def _ensure_public_resource_passthrough(
        self,
        resource: PangolinResource,
    ) -> PangolinResource:
        if not resource.sso and resource.enabled:
            return resource
        data = self._request(
            "POST",
            f"/public-resource/{resource.resource_id}",
            json={"sso": False, "enabled": True},
        )
        updated = self._parse(data, PangolinResource, "resource access update")
        if updated.sso or not updated.enabled:
            raise PangolinApiError(
                f"Pangolin resource {resource.resource_id} did not accept public access",
                status_code=200,
                retryable=True,
            )
        return updated

    def _ensure_targets(
        self,
        resource: PangolinResource,
        *,
        platform_site_ids: tuple[int, ...] | None = None,
    ) -> None:
        platform_site_ids = platform_site_ids or self._platform_site_ids()
        data = self._request(
            "GET",
            f"/public-resource/{resource.resource_id}/targets",
            params={"limit": "1000", "offset": "0"},
        )
        targets = self._parse(data, _TargetListData, "target listing").targets
        matching = tuple(
            target
            for target in targets
            if target.site_id in platform_site_ids
            and target.ip == self.platform_target_host
            and target.port == self.platform_target_port
            and target.method == "http"
        )
        if len(matching) != len(targets) or len({target.site_id for target in matching}) != len(
            matching
        ):
            raise InvalidInputError(
                f"Pangolin resource {resource.resource_id} has an unexpected target"
            )
        existing_site_ids = {target.site_id for target in matching}
        for target in matching:
            current = self._get_target(target.target_id)
            if current.enabled and self._target_health_matches(current):
                continue
            self._request(
                "POST",
                f"/target/{target.target_id}",
                json={
                    "siteId": target.site_id,
                    "ip": self.platform_target_host,
                    "method": "http",
                    "port": self.platform_target_port,
                    "enabled": True,
                    "hcHostname": self.platform_target_host,
                    "hcPort": self.platform_target_port,
                    **_TARGET_HEALTH,
                },
            )
        for platform_site_id in platform_site_ids:
            if platform_site_id in existing_site_ids:
                continue
            self._request(
                "PUT",
                f"/public-resource/{resource.resource_id}/target",
                json={
                    "siteId": platform_site_id,
                    "ip": self.platform_target_host,
                    "method": "http",
                    "port": self.platform_target_port,
                    "enabled": True,
                    "hcHostname": self.platform_target_host,
                    "hcPort": self.platform_target_port,
                    **_TARGET_HEALTH,
                },
                allowed_status_codes=(200, 201),
            )

    def _get_target(self, target_id: int) -> PangolinTarget:
        data = self._request("GET", f"/target/{target_id}")
        return self._parse(data, PangolinTarget, "target lookup")

    def _target_health_matches(self, target: PangolinTarget) -> bool:
        return (
            target.health_check_enabled
            and target.health_check_path == _TARGET_HEALTH["hcPath"]
            and target.health_check_scheme == _TARGET_HEALTH["hcScheme"]
            and target.health_check_mode == _TARGET_HEALTH["hcMode"]
            and target.health_check_hostname == self.platform_target_host
            and target.health_check_port == self.platform_target_port
            and target.health_check_interval == _TARGET_HEALTH["hcInterval"]
            and target.health_check_unhealthy_interval == _TARGET_HEALTH["hcUnhealthyInterval"]
            and target.health_check_timeout == _TARGET_HEALTH["hcTimeout"]
            and target.health_check_follow_redirects is _TARGET_HEALTH["hcFollowRedirects"]
            and target.health_check_method == _TARGET_HEALTH["hcMethod"]
            and target.health_check_status == _TARGET_HEALTH["hcStatus"]
            and target.health_check_healthy_threshold == _TARGET_HEALTH["hcHealthyThreshold"]
            and target.health_check_unhealthy_threshold == _TARGET_HEALTH["hcUnhealthyThreshold"]
        )

    def _required_dns_records(self, domain_id: str) -> tuple[DnsRecord, ...]:
        return tuple(
            DnsRecord(type=record.record_type, name=record.base_domain, value=record.value)
            for record in self._dns_records(domain_id)
            if not record.verified
        )

    def _require_platform_target(self) -> None:
        if not self.platform_target_host.strip():
            raise InvalidInputError("Pangolin platform target host is not configured")

    def _platform_site_ids(self) -> tuple[int, ...]:
        if not self.platform_site_ids:
            raise InvalidInputError("Pangolin platform site IDs are not configured")
        for ordinal, site_id in enumerate(self.platform_site_ids):
            site = self.get_site(site_id)
            if (
                site is None
                or not site.newt_id
                or site.name != f"{self.platform_identity_prefix}-{ordinal}"
            ):
                raise InvalidInputError(
                    f"Pangolin platform site {site_id} does not match ordinal {ordinal}"
                )
        return self.platform_site_ids

    def _platform_client_record_ids(self) -> tuple[int, ...]:
        if not self.platform_client_record_ids:
            raise InvalidInputError("Pangolin platform machine-client IDs are not configured")
        for ordinal, client_id in enumerate(self.platform_client_record_ids):
            client = self.get_client(client_id)
            if (
                client is None
                or not client.olm_id
                or client.name != f"{self.platform_identity_prefix}-{ordinal}"
            ):
                raise InvalidInputError(
                    f"Pangolin platform machine client {client_id} does not match ordinal {ordinal}"
                )
        return self.platform_client_record_ids

    def _sites(self, *, query: str) -> tuple[PangolinSite, ...]:
        data = self._request(
            "GET",
            f"/org/{quote(self.organization_id, safe='')}/sites",
            params={"pageSize": "100", "page": "1", "query": query},
        )
        return self._parse(data, _SiteListData, "site listing").sites

    def _clients(self, *, query: str) -> tuple[PangolinClientRecord, ...]:
        data = self._request(
            "GET",
            f"/org/{quote(self.organization_id, safe='')}/clients",
            params={"pageSize": "100", "page": "1", "query": query},
        )
        return self._parse(data, _ClientListData, "machine-client listing").clients

    def _domains(self) -> tuple[PangolinDomain, ...]:
        domains: list[PangolinDomain] = []
        limit = 1000
        while True:
            data = self._request(
                "GET",
                f"/org/{quote(self.organization_id, safe='')}/domains",
                params={"limit": str(limit), "offset": str(len(domains))},
            )
            page = self._parse(data, _DomainListData, "domain listing").domains
            domains.extend(page)
            if len(page) < limit:
                return tuple(domains)

    def _dns_records(self, domain_id: str) -> tuple[PangolinDnsRecord, ...]:
        data = self._request(
            "GET",
            f"{self._domain_path(domain_id)}/dns-records",
            allowed_status_codes=(200, 404),
            missing_is_none=True,
        )
        if data is None:
            return ()
        try:
            return TypeAdapter(tuple[PangolinDnsRecord, ...]).validate_python(data)
        except ValidationError as exc:
            raise PangolinApiError(
                "Pangolin returned unreadable DNS records",
                status_code=200,
                retryable=True,
            ) from exc

    def _domain_path(self, domain_id: str) -> str:
        return f"/org/{quote(self.organization_id, safe='')}/domain/{quote(domain_id, safe='')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, object] | None = None,
        params: dict[str, str] | None = None,
        allowed_status_codes: tuple[int, ...] = (200,),
        missing_is_none: bool = False,
    ) -> JsonValue:
        try:
            with httpx.Client(
                base_url=self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key.get_secret_value()}",
                    "Accept": "application/json",
                },
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = client.request(method, path, json=json, params=params)
        except httpx.RequestError as exc:
            raise UpstreamUnavailableError(f"Pangolin request failed for {method} {path}") from exc
        if missing_is_none and response.status_code == 404:
            return None
        if response.status_code not in allowed_status_codes:
            message = self._error_message(response)
            if response.status_code in {400, 422}:
                raise InvalidInputError(message)
            raise PangolinApiError(
                message,
                status_code=response.status_code,
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        if response.status_code == 404:
            return None
        if response.status_code == 204 or not response.content:
            return None
        try:
            envelope = _Envelope.model_validate_json(response.content)
        except ValidationError as exc:
            raise PangolinApiError(
                "Pangolin returned an unreadable response",
                status_code=response.status_code,
                retryable=True,
            ) from exc
        if not envelope.success or envelope.error:
            raise PangolinApiError(
                envelope.message or "Pangolin rejected the request",
                status_code=envelope.status,
                retryable=envelope.status >= 500 or envelope.status == 429,
            )
        return envelope.data

    @staticmethod
    def _parse[T: BaseModel](data: JsonValue, model: type[T], operation: str) -> T:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise PangolinApiError(
                f"Pangolin returned unreadable data for {operation}",
                status_code=200,
                retryable=True,
            ) from exc

    @staticmethod
    def _error_message(response: httpx.Response) -> str:
        try:
            envelope = _Envelope.model_validate_json(response.content)
        except ValidationError:
            return f"Pangolin rejected the request ({response.status_code})"
        return envelope.message or f"Pangolin rejected the request ({response.status_code})"


def _provider_hostname_id(dns_mode: CustomDomainDnsMode, domain_id: str) -> str:
    return f"{dns_mode.value}:{domain_id}"


def _parse_provider_hostname_id(value: str) -> tuple[CustomDomainDnsMode, str]:
    mode_value, separator, domain_id = value.partition(":")
    if not separator or not domain_id:
        raise InvalidInputError("Pangolin hostname ID is invalid")
    try:
        mode = CustomDomainDnsMode(mode_value)
    except ValueError as exc:
        raise InvalidInputError("Pangolin hostname DNS mode is invalid") from exc
    return mode, domain_id


def _resource_name_prefix(domain_id: str) -> str:
    return f"lazycloud-{domain_id}-"


def _resource_name(domain_id: str, subdomain: str | None) -> str:
    scope = "wildcard" if subdomain == "*" else "apex"
    return f"{_resource_name_prefix(domain_id)}{scope}"


def _is_agent_private_resource(name: str) -> bool:
    site_id = name.removeprefix("lazycloud-site-").removesuffix("-route-proxy")
    return site_id.isdecimal() and name == f"lazycloud-site-{site_id}-route-proxy"


__all__ = [
    "PangolinApiError",
    "PangolinClient",
    "PangolinClientDefaults",
    "PangolinClientRecord",
    "PangolinCreatedClient",
    "PangolinCreatedSite",
    "PangolinDnsRecord",
    "PangolinDomain",
    "PangolinPrivateResource",
    "PangolinPrivateResourceClient",
    "PangolinResource",
    "PangolinSite",
    "PangolinTarget",
]
