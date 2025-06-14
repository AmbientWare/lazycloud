from pydantic import BaseModel, field_validator
from typing import List, Optional, Dict
import re
from enum import Enum


class ImageTypes(Enum):
    UBUNTU_22_04 = "ubuntu2204"


# NOTE: we maintain a public image that is pre-built and can be used to deploy machines faster
IMAGE_MAP = {
    ImageTypes.UBUNTU_22_04: "cmclean165/ubuntu2204:latest",
}


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


class GPUPricingRow(BaseModel):
    model: str  # e.g. "A10", "L40S"
    price_hour: Optional[float] = None

    @field_validator("price_hour", mode="before")
    def clean_price(cls, v):
        if isinstance(v, str):
            # Remove currency symbols and commas
            cleaned_v = re.sub(r"[\$,]", "", v)
            try:
                return float(cleaned_v)
            except (ValueError, TypeError):
                return None
        return v


class GPUPricingTable(BaseModel):
    gpu_rows: List[GPUPricingRow]


class PricingData(BaseModel):
    markups: Markups
    pricing_table: PricingTable
    gpu_pricing: Optional[GPUPricingTable] = None


class GPUInfo(BaseModel):
    regions: List[str]


class PlatformOptions(BaseModel):
    regions: List[str]
    compute: Dict[int, List[int]]
    gpu: Dict[str, GPUInfo]
