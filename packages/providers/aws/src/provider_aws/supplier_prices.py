from pydantic import BaseModel, ConfigDict, Field


class AwsRegionalPrices(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gp3_gib_monthly_micros: int = Field(ge=0)
    public_ipv4_hourly_micros: int = Field(ge=0)

    def root_disk_hourly_micros(self, gib: int) -> int:
        # Normalize the monthly storage rate to AWS's 30-day pricing example.
        return (gib * self.gp3_gib_monthly_micros + 719) // 720
