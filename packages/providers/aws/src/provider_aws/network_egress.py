from collections.abc import Mapping
from ipaddress import ip_network
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field
from shared.network_egress import NetworkEgressRouteEvidence


class NetworkFilter(TypedDict):
    Name: str
    Values: list[str]


class AwsNetworkEvidenceClient(Protocol):
    def describe_instances(self, *, InstanceIds: list[str]) -> Mapping[str, object]: ...
    def describe_route_tables(
        self, *, Filters: list[NetworkFilter], NextToken: str = ""
    ) -> Mapping[str, object]: ...
    def describe_vpc_endpoints(self, *, VpcEndpointIds: list[str]) -> Mapping[str, object]: ...
    def describe_managed_prefix_lists(
        self, *, PrefixListIds: list[str]
    ) -> Mapping[str, object]: ...
    def get_managed_prefix_list_entries(
        self, *, PrefixListId: str, NextToken: str = ""
    ) -> Mapping[str, object]: ...


class _AwsModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Instance(_AwsModel):
    id: str = Field(alias="InstanceId")
    vpc: str = Field(alias="VpcId")
    subnet: str = Field(alias="SubnetId")


class _Reservation(_AwsModel):
    instances: tuple[_Instance, ...] = Field(alias="Instances")


class _Instances(_AwsModel):
    reservations: tuple[_Reservation, ...] = Field(alias="Reservations")


class _Association(_AwsModel):
    subnet: str = Field(default="", alias="SubnetId")
    main: bool = Field(default=False, alias="Main")


class _Route(_AwsModel):
    prefix_list: str = Field(default="", alias="DestinationPrefixListId")
    gateway: str = Field(default="", alias="GatewayId")
    state: str = Field(alias="State")


class _RouteTable(_AwsModel):
    id: str = Field(alias="RouteTableId")
    associations: tuple[_Association, ...] = Field(alias="Associations")
    routes: tuple[_Route, ...] = Field(alias="Routes")


class _RouteTables(_AwsModel):
    tables: tuple[_RouteTable, ...] = Field(alias="RouteTables")
    next_token: str = Field(default="", alias="NextToken")


class _Endpoint(_AwsModel):
    id: str = Field(alias="VpcEndpointId")
    vpc: str = Field(alias="VpcId")
    kind: str = Field(alias="VpcEndpointType")
    service: str = Field(alias="ServiceName")
    state: str = Field(alias="State")
    route_tables: tuple[str, ...] = Field(default=(), alias="RouteTableIds")


class _Endpoints(_AwsModel):
    endpoints: tuple[_Endpoint, ...] = Field(alias="VpcEndpoints")


class _PrefixList(_AwsModel):
    id: str = Field(alias="PrefixListId")
    name: str = Field(alias="PrefixListName")
    owner: str = Field(alias="OwnerId")


class _PrefixLists(_AwsModel):
    lists: tuple[_PrefixList, ...] = Field(alias="PrefixLists")


class _Entry(_AwsModel):
    cidr: str = Field(alias="Cidr")


class _Entries(_AwsModel):
    entries: tuple[_Entry, ...] = Field(alias="Entries")
    next_token: str = Field(default="", alias="NextToken")


def same_region_storage_destinations(
    client: AwsNetworkEvidenceClient,
    *,
    region: str,
    instance_id: str,
    vpc_id: str,
    subnet_ids: tuple[str, ...],
) -> NetworkEgressRouteEvidence:
    response = _Instances.model_validate(client.describe_instances(InstanceIds=[instance_id]))
    instances = [
        instance for reservation in response.reservations for instance in reservation.instances
    ]
    if (
        len(instances) != 1
        or instances[0].id != instance_id
        or instances[0].vpc != vpc_id
        or instances[0].subnet not in subnet_ids
    ):
        raise ValueError(
            "AWS network evidence requires the owned instance in its configured network"
        )
    subnet_id = instances[0].subnet
    tables: list[_RouteTable] = []
    token = ""
    seen: set[str] = set()
    while True:
        page = _RouteTables.model_validate(
            client.describe_route_tables(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}],
                **({"NextToken": token} if token else {}),
            )
        )
        tables.extend(page.tables)
        token = page.next_token
        if not token:
            break
        if token in seen:
            raise ValueError("AWS route table pagination repeated a token")
        seen.add(token)
    explicit = [table for table in tables if any(a.subnet == subnet_id for a in table.associations)]
    effective = explicit or [table for table in tables if any(a.main for a in table.associations)]
    if len(effective) != 1:
        raise ValueError("AWS subnet has no unique effective route table")
    table = effective[0]
    service = f"com.amazonaws.{region}.s3"
    destinations: set[str] = set()
    for route in table.routes:
        if (
            route.state != "active"
            or not route.prefix_list
            or not route.gateway.startswith("vpce-")
        ):
            continue
        endpoints = _Endpoints.model_validate(
            client.describe_vpc_endpoints(VpcEndpointIds=[route.gateway])
        ).endpoints
        if len(endpoints) != 1:
            raise ValueError("AWS route endpoint evidence is incomplete")
        endpoint = endpoints[0]
        if (
            endpoint.id != route.gateway
            or endpoint.vpc != vpc_id
            or endpoint.kind != "Gateway"
            or endpoint.service != service
            or endpoint.state != "available"
            or table.id not in endpoint.route_tables
        ):
            continue
        lists = _PrefixLists.model_validate(
            client.describe_managed_prefix_lists(PrefixListIds=[route.prefix_list])
        ).lists
        if (
            len(lists) != 1
            or lists[0].id != route.prefix_list
            or lists[0].owner != "AWS"
            or lists[0].name != service
        ):
            raise ValueError("AWS S3 route does not reference the regional AWS-managed prefix list")
        token = ""
        seen = set()
        while True:
            entries = _Entries.model_validate(
                client.get_managed_prefix_list_entries(
                    PrefixListId=route.prefix_list,
                    **({"NextToken": token} if token else {}),
                )
            )
            destinations.update(entry.cidr for entry in entries.entries)
            token = entries.next_token
            if not token:
                break
            if token in seen:
                raise ValueError("AWS prefix list pagination repeated a token")
            seen.add(token)
    if not destinations:
        raise ValueError("AWS instance has no verified same-region S3 gateway route")
    versions: tuple[Literal[4, 6], ...] = (4, 6)
    return NetworkEgressRouteEvidence(
        excluded_destinations=tuple(sorted(destinations)),
        verified_ip_versions=tuple(
            version
            for version in versions
            if any(ip_network(cidr).version == version for cidr in destinations)
        ),
    )
