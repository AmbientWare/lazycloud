from collections.abc import Mapping
from datetime import UTC, datetime

from provider_aws.spot_prices import load_aws_spot_quotes
from shared.aws_connections import AwsAccountNetwork


def test_spot_quote_uses_latest_price_across_pages_without_rounding_cost_down() -> None:
    now = datetime(2026, 9, 9, tzinfo=UTC)

    class PriceClient:
        def describe_subnets(self, *, SubnetIds: list[str]) -> Mapping[str, object]:
            return {
                "Subnets": [
                    {"SubnetId": subnet, "VpcId": "vpc-owned", "AvailabilityZoneId": "use1-az1"}
                    for subnet in SubnetIds
                ]
            }

        def describe_spot_price_history(self, **kwargs: object) -> Mapping[str, object]:
            first_page = not kwargs["NextToken"]
            price: dict[str, str | datetime] = {
                "InstanceType": "m6a.8xlarge",
                "AvailabilityZoneId": "use1-az1",
                "ProductDescription": "Linux/UNIX",
                "SpotPrice": "0.4" if first_page else "0.6000001",
                "Timestamp": "2026-09-08T00:00:00Z" if first_page else now,
            }
            return {
                "SpotPriceHistory": [price],
                "NextToken": "next" if first_page else "",
            }

    market = load_aws_spot_quotes(
        PriceClient(),
        network=AwsAccountNetwork(
            vpc_id="vpc-owned",
            subnet_ids=("subnet-first", "subnet-second"),
            security_group_id="sg-owned",
        ),
        instance_types=("m6a.8xlarge",),
        observed_at=now,
    )

    quotes = market.quotes
    assert len(quotes) == 1
    assert quotes[0].compute_hourly_micros == 600_001
    assert quotes[0].availability_zone == "use1-az1"
    assert quotes[0].effective_at == now
    assert quotes[0].observed_at == now
