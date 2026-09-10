from types import MappingProxyType
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class AwsRegionalPrices(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gp3_gib_monthly_micros: int = Field(ge=0)
    public_ipv4_hourly_micros: int = Field(ge=0)
    instance_hourly_micros: dict[str, Annotated[int, Field(gt=0)]] = Field(default_factory=dict)

    def root_disk_hourly_micros(self, gib: int) -> int:
        # Normalize the monthly storage rate to AWS's 30-day pricing example.
        return (gib * self.gp3_gib_monthly_micros + 719) // 720


# Reviewed September 10, 2026. These are regional on-demand quotes, not live observations.
AWS_REGIONAL_PRICES = MappingProxyType(
    {
        "us-east-1": AwsRegionalPrices(
            gp3_gib_monthly_micros=80_000,
            public_ipv4_hourly_micros=5_000,
            instance_hourly_micros={
                "c6a.2xlarge": 306000,
                "c6a.4xlarge": 612000,
                "c6a.8xlarge": 1224000,
                "c6i.8xlarge": 1360000,
                "g4dn.12xlarge": 3912000,
                "g4dn.2xlarge": 752000,
                "g4dn.4xlarge": 1204000,
                "g4dn.metal": 7824000,
                "g4dn.xlarge": 526000,
                "g5.12xlarge": 5672000,
                "g5.16xlarge": 4096000,
                "g5.24xlarge": 8144000,
                "g5.2xlarge": 1212000,
                "g5.48xlarge": 16288000,
                "g5.4xlarge": 1624000,
                "g5.8xlarge": 2448000,
                "g5.xlarge": 1006000,
                "g6.12xlarge": 4601600,
                "g6.16xlarge": 3396800,
                "g6.24xlarge": 6675200,
                "g6.2xlarge": 977600,
                "g6.48xlarge": 13350400,
                "g6.4xlarge": 1323200,
                "g6.8xlarge": 2014400,
                "g6.xlarge": 804800,
                "m6a.2xlarge": 345600,
                "m6a.4xlarge": 691200,
                "m6a.8xlarge": 1382400,
                "m7i.12xlarge": 2419200,
                "m7i.16xlarge": 3225600,
                "m7i.2xlarge": 403200,
                "m7i.4xlarge": 806400,
                "m7i.8xlarge": 1612800,
                "r6a.2xlarge": 453600,
                "r6a.4xlarge": 907200,
                "r6a.8xlarge": 1814400,
            },
        ),
    }
)
