from collections.abc import Mapping

import pytest
from provider_aws.network_egress import NetworkFilter, same_region_storage_destinations
from pydantic import JsonValue


class NetworkInventory:
    explicit_subnet_route = False
    service = "com.amazonaws.us-east-1.s3"

    def describe_instances(self, *, InstanceIds: list[str]) -> Mapping[str, JsonValue]:
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-owned",
                            "VpcId": "vpc-owned",
                            "SubnetId": "subnet-owned",
                        }
                    ]
                }
            ]
        }

    def describe_route_tables(
        self, *, Filters: list[NetworkFilter], NextToken: str = ""
    ) -> Mapping[str, JsonValue]:
        return {
            "RouteTables": [
                {
                    "RouteTableId": "rt-main",
                    "Associations": [{"Main": True}],
                    "Routes": [
                        {
                            "State": "active",
                            "DestinationPrefixListId": "pl-s3",
                            "GatewayId": "vpce-s3",
                        },
                    ],
                },
                {
                    "RouteTableId": "rt-other",
                    "Associations": [
                        {
                            "SubnetId": "subnet-owned"
                            if self.explicit_subnet_route
                            else "subnet-other",
                        }
                    ],
                    "Routes": [],
                },
            ]
        }

    def describe_vpc_endpoints(self, *, VpcEndpointIds: list[str]) -> Mapping[str, JsonValue]:
        return {
            "VpcEndpoints": [
                {
                    "VpcEndpointId": "vpce-s3",
                    "VpcId": "vpc-owned",
                    "VpcEndpointType": "Gateway",
                    "ServiceName": self.service,
                    "State": "available",
                    "RouteTableIds": ["rt-main"],
                }
            ]
        }

    def describe_managed_prefix_lists(self, *, PrefixListIds: list[str]) -> Mapping[str, JsonValue]:
        return {
            "PrefixLists": [
                {
                    "PrefixListId": "pl-s3",
                    "PrefixListName": self.service,
                    "OwnerId": "AWS",
                }
            ]
        }

    def get_managed_prefix_list_entries(
        self, *, PrefixListId: str, NextToken: str = ""
    ) -> Mapping[str, JsonValue]:
        if NextToken:
            return {"Entries": [{"Cidr": "52.216.0.0/15"}]}
        return {"Entries": [{"Cidr": "16.182.0.0/16"}], "NextToken": "remaining"}


def test_storage_exemption_requires_the_instances_effective_same_region_gateway_route() -> None:
    inventory = NetworkInventory()
    proof = same_region_storage_destinations(
        inventory,
        region="us-east-1",
        instance_id="i-owned",
        vpc_id="vpc-owned",
        subnet_ids=("subnet-owned",),
    )
    assert proof.excluded_destinations == ("16.182.0.0/16", "52.216.0.0/15")
    assert proof.verified_ip_versions == (4,)

    inventory.explicit_subnet_route = True
    with pytest.raises(ValueError, match="no verified same-region S3 gateway route"):
        same_region_storage_destinations(
            inventory,
            region="us-east-1",
            instance_id="i-owned",
            vpc_id="vpc-owned",
            subnet_ids=("subnet-owned",),
        )

    inventory.explicit_subnet_route = False
    inventory.service = "com.amazonaws.us-west-2.s3"
    with pytest.raises(ValueError, match="no verified same-region S3 gateway route"):
        same_region_storage_destinations(
            inventory,
            region="us-east-1",
            instance_id="i-owned",
            vpc_id="vpc-owned",
            subnet_ids=("subnet-owned",),
        )
