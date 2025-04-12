from pydantic import BaseModel, field_validator
from typing import List, Optional
import re


class Region(BaseModel):
    region: str
    markup: float


class Markups(BaseModel):
    regions: List[Region]


class PricingRow(BaseModel):
    preset_group: str
    cpus: int
    ram: int
    ram_units: str
    price_sec: Optional[float] = None
    price_hour: Optional[float] = None
    price_month: Optional[float] = None

    @field_validator("price_sec", "price_hour", "price_month", mode="before")
    def clean_price(cls, v):
        if isinstance(v, str):
            # Remove currency symbols and commas
            cleaned_v = re.sub(r"[\$,]", "", v)
            try:
                return float(cleaned_v)

            except (ValueError, TypeError):
                return None  # Or raise ValueError("Invalid price format")

        return v  # Return as is if already float or None


class PricingTable(BaseModel):
    pricing_rows: List[PricingRow]


class PricingData(BaseModel):
    markups: Markups
    pricing_table: PricingTable


class PresetGroup(BaseModel):
    name: str
    cpus: List[int]
    ram: List[int]


class PlatformOptions(BaseModel):
    regions: List[str]
    preset_groups: List[PresetGroup]
