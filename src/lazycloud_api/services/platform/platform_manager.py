import asyncio
from typing import Dict, List, Optional, Any, Sequence, cast
import re

import aiohttp
from bs4 import BeautifulSoup, Tag
import pandas as pd
from pydantic import BaseModel, ValidationError  # Added BaseModel for UnitPrice
from redis.asyncio import Redis

from lazycloud_api.config import app_config
from lazycloud_api.services.platform.schemas import (
    GPUPricingRow,
    GPUPricingTable,
    Markups,
    PlatformOptions,
    PricingData,
    PricingRow,
    PricingTable,
    Region,
    GPUInfo,
)
from lazycloud_api.services.fly.schemas import RESOURCE_MAP, FlyRegion

# Constants
FLY_PRICING_URL = "https://fly.io/docs/about/pricing/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
EWR_TABLE_ID = "started-machines-pricing-matrix-ewr"  # This is assumed to be the region with markup 1.0

# Redis keys
REDIS_KEYS = {
    "region_markups": "pricing:region_markups",
    "pricing_table": "pricing:pricing_table",
    "gpu_pricing": "pricing:gpu_pricing",
    "single_cpu_price": "pricing:single_cpu_price",
    "single_memory_price_gb": "pricing:single_memory_price_gb",
    "upcharge": "pricing:upcharge",
}

# GPU name mapping between pricing table and resource map
GPU_NAME_MAP = {
    "A10": "a10",
    "L40S": "l40s",
    "A100 40G PCIe": "a100-40gb",
    "A100 80G SXM": "a100-80gb",
}


# Make these models public for API use
class UnitPrice(BaseModel):
    price_sec: Optional[float] = None
    price_hour: Optional[float] = None
    price_month: Optional[float] = None


class UpchargeData(BaseModel):
    value: float


class UnitPricing(BaseModel):
    """Model for combined CPU and memory unit pricing"""

    cpu: UnitPrice
    memory_gb: UnitPrice


class PlatformManager:
    def __init__(
        self,
        url: str = FLY_PRICING_URL,
        user_agent: str = USER_AGENT,
        performance_only: bool = True,
    ):
        self.url = url
        self.headers = {"User-Agent": user_agent}
        self.performance_only = performance_only

        # Internal state
        self.soup: Optional[BeautifulSoup] = None
        self._region_markups: Optional[Markups] = None
        self._pricing_table: Optional[PricingTable] = None
        self._gpu_pricing: Optional[GPUPricingTable] = None

        # These will store the newly calculated prices
        self._single_cpu_price: Optional[UnitPrice] = None
        self._calculated_memory_price_gb: Optional[UnitPrice] = None

        # Initialize Redis client
        self._redis_client = Redis.from_url(app_config.REDIS_URL)
        if not self._redis_client:
            raise RuntimeError("Failed to connect to Redis")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.cleanup()

    @staticmethod
    def _sort_ram_values(ram_values: List[int]) -> List[int]:
        """Sort RAM values from smallest to largest."""
        return sorted(ram_values)

    async def cleanup(self) -> None:
        """Cleanup Redis connection."""
        if self._redis_client:
            await self._redis_client.aclose()

    async def _fetch_html(self) -> bool:
        """Fetch and parse HTML content."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url, headers=self.headers) as response:
                    response.raise_for_status()
                    html_content = await response.text()
                    self.soup = BeautifulSoup(html_content, "html.parser")
                    return True

        except aiohttp.ClientError as e:
            print(f"Error fetching URL {self.url}: {e}")
            self.soup = None
            return False

    async def _extract_region_markups(self) -> Markups:
        """Extract region markup data from HTML."""
        if not self.soup:
            raise ValueError("BeautifulSoup not initialized")

        valid_regions = {region.value for region in FlyRegion}

        for script in self.soup.find_all("script", type="text/javascript"):
            if not isinstance(script, Tag) or not script.string:
                continue

            if "const regionMarkups = {" not in script.string:
                continue

            match = re.search(
                r"const\s+regionMarkups\s*=\s*(\{.*?\});",
                script.string,
                re.DOTALL | re.MULTILINE,
            )
            if not match:
                continue

            try:
                markups_raw = re.findall(r'"(\w+)":\s*([\d.]+)', match.group(1))
                regions = [
                    Region(
                        region=key,
                        markup=float(value),
                    )
                    for key, value in markups_raw
                    if key in valid_regions
                ]
                return Markups(regions=regions)
            except (ValidationError, ValueError) as e:
                print(f"Error parsing markups: {e}")

        raise ValueError("Could not find region markups data")

    async def _extract_pricing_table(self) -> PricingTable:
        """Extract machine pricing data from HTML (specifically EWR table for base pricing)."""
        if not self.soup:
            raise ValueError("BeautifulSoup not initialized")

        pricing_rows = []
        # EWR_TABLE_ID is used here, which corresponds to a region with markup 1.0
        table_div = self.soup.find("div", id=EWR_TABLE_ID)
        if not table_div or not isinstance(table_div, Tag):
            raise ValueError(f"Could not find pricing table with id '{EWR_TABLE_ID}'")

        table = table_div.find("table")
        if not table or not isinstance(table, Tag):
            raise ValueError("Could not find pricing table")

        tbody = table.find("tbody")
        if not tbody or not isinstance(tbody, Tag):
            raise ValueError("Could not find pricing table body")

        current_machine_type = None
        current_cpus = None

        for row in tbody.find_all("tr"):
            if not isinstance(row, Tag):
                continue

            cells = cast(List[Tag], row.find_all(["th", "td"]))
            if not cells or not isinstance(cells[0], Tag):
                continue

            try:
                pricing_row = self._parse_pricing_row(
                    cells, current_machine_type, current_cpus
                )
                if pricing_row:
                    if isinstance(cells[0], Tag) and cells[0].name == "th":
                        current_machine_type = pricing_row.preset_group
                        current_cpus = str(pricing_row.cpus)
                    # Filter for performance machines if self.performance_only is True
                    if (
                        self.performance_only
                        and "performance" not in pricing_row.preset_group.lower()
                    ):
                        # If we are in a new non-performance group, reset current type to avoid adding its data rows
                        if (
                            isinstance(cells[0], Tag)
                            and cells[0].name == "th"
                            and cells[0].has_attr("rowspan")
                        ):
                            current_machine_type = None
                            current_cpus = None
                        continue  # Skip non-performance rows if performance_only
                    elif (
                        not self.performance_only
                        or "performance" in pricing_row.preset_group.lower()
                    ):
                        pricing_rows.append(pricing_row)

            except (ValueError, ValidationError) as e:
                print(f"Error parsing row: {e}")
                continue

        if not pricing_rows:
            raise ValueError("No valid pricing data extracted for EWR table")

        return PricingTable(pricing_rows=pricing_rows)

    def _parse_pricing_row(
        self,
        cells: Sequence[Tag],
        current_machine_type: Optional[str],
        current_cpus: Optional[str],
    ) -> Optional[PricingRow]:
        """Parse a single pricing table row."""
        first_cell = cells[0]

        # Handle header rows
        if (
            isinstance(first_cell, Tag)
            and first_cell.name == "th"
            and first_cell.has_attr("rowspan")
        ):
            if len(cells) < 6:
                return None

            machine_type_text = first_cell.get_text(strip=True)

            cpu_count = cells[1].get_text(strip=True).split()[0]
            ram_text = cells[2].get_text(strip=True)

            return self._create_pricing_row(
                machine_type_text,
                cpu_count,
                ram_text,
                cells[3].get_text(strip=True),
                cells[4].get_text(strip=True),
                cells[5].get_text(strip=True),
            )

        # Handle data rows
        elif (
            isinstance(first_cell, Tag)
            and first_cell.name == "td"
            and current_machine_type
            and current_cpus
        ):
            if len(cells) < 4:
                return None

            ram_text = cells[-4].get_text(
                strip=True
            )  # cells[0] is RAM for data rows after first
            return self._create_pricing_row(
                current_machine_type,
                current_cpus,
                ram_text,
                cells[-3].get_text(strip=True),
                cells[-2].get_text(strip=True),
                cells[-1].get_text(strip=True),
            )

        return None

    @staticmethod
    def _create_pricing_row(
        machine_type: str,
        cpu_count: str,
        ram_text: str,
        price_sec: str,
        price_hour: str,
        price_month: str,
    ) -> PricingRow:
        """Create a PricingRow from raw values."""
        try:
            cpu_value = int(cpu_count)
        except ValueError:
            # Handle cases like "1 shared" or "1 performance" if cpu_count is not just a number
            match_cpu = re.match(r"(\d+)", cpu_count)
            if match_cpu:
                cpu_value = int(match_cpu.group(1))
            else:
                raise ValueError(f"Invalid CPU format: {cpu_count}")

        ram_match = re.match(r"(\d+)([A-Za-z]+)", ram_text)
        if not ram_match:
            raise ValueError(f"Invalid RAM format: {ram_text}")

        ram_value = int(ram_match.group(1))
        ram_units = ram_match.group(2)

        if ram_units not in ["MB", "GB", "TB"]:
            raise ValueError(f"Invalid RAM unit: {ram_units}")

        if not any([price_sec, price_hour, price_month]):
            raise ValueError(f"No valid prices found for {machine_type}")

        # Convert price strings to floats
        try:
            price_sec_float = (
                float(price_sec.replace("$", ""))
                if price_sec and price_sec != "-"
                else None
            )
            price_hour_float = (
                float(price_hour.replace("$", ""))
                if price_hour and price_hour != "-"
                else None
            )
            price_month_float = (
                float(price_month.replace("$", ""))
                if price_month and price_month != "-"
                else None
            )
        except ValueError as e:
            raise ValueError(f"Invalid price format: {e}")

        return PricingRow(
            preset_group=machine_type,
            cpus=cpu_value,
            ram=ram_value,
            ram_units=ram_units,
            price_sec=price_sec_float,
            price_hour=price_hour_float,
            price_month=price_month_float,
        )

    async def _extract_gpu_pricing(self) -> GPUPricingTable:
        """Extract GPU pricing data from HTML."""
        if not self.soup:
            raise ValueError("BeautifulSoup not initialized")

        gpu_rows = []
        for p in self.soup.find_all("p"):
            if not isinstance(p, Tag):
                continue

            if p.get_text(strip=True) != "On-demand GPU pricing:":
                continue

            pricing_list = p.find_next_sibling("ul")
            if not pricing_list or not isinstance(pricing_list, Tag):
                continue

            for item in pricing_list.find_all("li"):
                if not isinstance(item, Tag):
                    continue

                try:
                    text = item.get_text(strip=True)
                    if ":" not in text:
                        continue

                    model, price_text = text.split(":", 1)
                    price_text = price_text.replace("per GPU", "").strip()

                    price_match = re.search(r"\$?([\d.]+)/hr", price_text)
                    if not price_match:
                        continue

                    gpu_rows.append(
                        GPUPricingRow(
                            model=model.strip(), price_hour=float(price_match.group(1))
                        )
                    )
                except (ValueError, ValidationError) as e:
                    print(f"Error parsing GPU price: {e}")
                    continue

            break

        return GPUPricingTable(gpu_rows=gpu_rows)

    def _calculate_cpu_memory_prices(
        self, pricing_table: PricingTable
    ) -> tuple[UnitPrice, UnitPrice]:
        """
        Calculates the price for 1 performance CPU and 1GB of performance memory
        based on specific machine configurations from the pricing table (assumed to be markup 1.0).
        """
        row_1cpu_2gb: Optional[PricingRow] = None
        row_1cpu_4gb: Optional[PricingRow] = None

        # Find the required performance machine configurations
        for row in pricing_table.pricing_rows:
            if row.preset_group.lower() == "performance-1x" and row.cpus == 1:
                if row.ram == 2 and row.ram_units == "GB":
                    row_1cpu_2gb = row
                elif row.ram == 4 and row.ram_units == "GB":
                    row_1cpu_4gb = row
            # Optimization: if both found, no need to iterate further
            if row_1cpu_2gb and row_1cpu_4gb:
                break

        if not row_1cpu_2gb:
            raise ValueError(
                "Pricing for 1 performance CPU, 2GB RAM (performance-1x) not found in EWR table."
            )
        if not row_1cpu_4gb:
            raise ValueError(
                "Pricing for 1 performance CPU, 4GB RAM (performance-1x) not found in EWR table."
            )

        # --- Calculate Price per 1GB RAM ---
        price_1gb_ram_sec: Optional[float] = None
        price_1gb_ram_hour: Optional[float] = None
        price_1gb_ram_month: Optional[float] = None
        price_2gb_ram_sec: Optional[float] = None
        price_2gb_ram_hour: Optional[float] = None
        price_2gb_ram_month: Optional[float] = None

        # Price of 2GB RAM = (Price of 1CPU+4GB machine) - (Price of 1CPU+2GB machine)
        if row_1cpu_4gb.price_sec is not None and row_1cpu_2gb.price_sec is not None:
            price_2gb_ram_sec = row_1cpu_4gb.price_sec - row_1cpu_2gb.price_sec
            price_1gb_ram_sec = price_2gb_ram_sec / 2
        if row_1cpu_4gb.price_hour is not None and row_1cpu_2gb.price_hour is not None:
            price_2gb_ram_hour = row_1cpu_4gb.price_hour - row_1cpu_2gb.price_hour
            price_1gb_ram_hour = price_2gb_ram_hour / 2
        if (
            row_1cpu_4gb.price_month is not None
            and row_1cpu_2gb.price_month is not None
        ):
            price_2gb_ram_month = row_1cpu_4gb.price_month - row_1cpu_2gb.price_month
            price_1gb_ram_month = price_2gb_ram_month / 2

        self._calculated_memory_price_gb = UnitPrice(
            price_sec=price_1gb_ram_sec,
            price_hour=price_1gb_ram_hour,
            price_month=price_1gb_ram_month,
        )

        # --- Calculate Price per 1 CPU ---
        # Price of 1 CPU = (Price of 1CPU+2GB machine) - (Price of 2GB RAM)
        price_1cpu_sec: Optional[float] = None
        price_1cpu_hour: Optional[float] = None
        price_1cpu_month: Optional[float] = None

        if (
            row_1cpu_2gb.price_sec is not None and price_2gb_ram_sec is not None
        ):  # Use price_2gb_ram here
            price_1cpu_sec = row_1cpu_2gb.price_sec - price_2gb_ram_sec
        if row_1cpu_2gb.price_hour is not None and price_2gb_ram_hour is not None:
            price_1cpu_hour = row_1cpu_2gb.price_hour - price_2gb_ram_hour
        if row_1cpu_2gb.price_month is not None and price_2gb_ram_month is not None:
            price_1cpu_month = row_1cpu_2gb.price_month - price_2gb_ram_month

        self._single_cpu_price = UnitPrice(
            price_sec=price_1cpu_sec,
            price_hour=price_1cpu_hour,
            price_month=price_1cpu_month,
        )

        return self._single_cpu_price, self._calculated_memory_price_gb

    async def _save_to_redis(self, key: str, data: Any) -> None:
        """Save data to Redis."""
        await self._redis_client.set(key, data.model_dump_json())

    async def _load_from_redis(self, key: str, model_class: type) -> Any:
        """Load data from Redis."""
        data = await self._redis_client.get(key)
        if not data:
            raise ValueError(f"No data found in Redis for key: {key}")
        return model_class.model_validate_json(data.decode())

    async def scrape(self) -> PricingData:
        """Perform complete scraping process."""
        if not await self._fetch_html() or not self.soup:
            # Return empty data if fetching fails
            self._region_markups = Markups(regions=[])
            self._pricing_table = PricingTable(pricing_rows=[])
            self._gpu_pricing = GPUPricingTable(gpu_rows=[])
        else:
            self._region_markups = await self._extract_region_markups()
            self._pricing_table = await self._extract_pricing_table()
            self._gpu_pricing = await self._extract_gpu_pricing()

        return PricingData(
            markups=self._region_markups,
            pricing_table=self._pricing_table,
            gpu_pricing=self._gpu_pricing,
        )

    async def update_pricing_data(self) -> None:
        """Update all pricing data in Redis, including calculated CPU/Memory prices."""
        pricing_data = await self.scrape()

        # Create tasks for all Redis saves
        tasks = [
            self._save_to_redis(REDIS_KEYS["region_markups"], pricing_data.markups),
            self._save_to_redis(
                REDIS_KEYS["pricing_table"], pricing_data.pricing_table
            ),
            self._save_to_redis(REDIS_KEYS["gpu_pricing"], pricing_data.gpu_pricing),
            self._save_to_redis(
                REDIS_KEYS["upcharge"],
                UpchargeData(value=app_config.LAZYCLOUD_UPCHARGE),
            ),
        ]

        # Calculate and save CPU/Memory prices if possible
        if pricing_data.pricing_table and pricing_data.pricing_table.pricing_rows:
            try:
                self._calculate_cpu_memory_prices(pricing_data.pricing_table)
                if self._single_cpu_price:
                    tasks.append(
                        self._save_to_redis(
                            REDIS_KEYS["single_cpu_price"],
                            self._single_cpu_price,
                        )
                    )
                if self._calculated_memory_price_gb:
                    tasks.append(
                        self._save_to_redis(
                            REDIS_KEYS["single_memory_price_gb"],
                            self._calculated_memory_price_gb,
                        )
                    )
            except ValueError as e:
                print(f"Could not calculate CPU/Memory prices: {e}")

        await asyncio.gather(*tasks)
        print("Pricing data update completed.")

    async def get_latest_pricing_data(self) -> PricingData:
        """Get latest pricing data from Redis or scrape if not available."""
        try:
            pricing_table = await self._load_from_redis(
                REDIS_KEYS["pricing_table"], PricingTable
            )
            markups = await self._load_from_redis(REDIS_KEYS["region_markups"], Markups)
            gpu_pricing = await self._load_from_redis(
                REDIS_KEYS["gpu_pricing"], GPUPricingTable
            )

            return PricingData(
                markups=markups, pricing_table=pricing_table, gpu_pricing=gpu_pricing
            )
        except ValueError:
            # If any key is missing, rescrape everything
            print("Data not found in Redis or error loading, scraping from web...")
            pricing_data_scraped = await self.scrape()
            # After scraping, also attempt to calculate and store derived prices
            if (
                pricing_data_scraped.pricing_table
                and pricing_data_scraped.pricing_table.pricing_rows
            ):
                try:
                    # Populate instance attributes for derived prices
                    self._calculate_cpu_memory_prices(
                        pricing_data_scraped.pricing_table
                    )
                except ValueError as e:
                    print(
                        f"Could not calculate CPU/Memory prices during fallback scrape: {e}"
                    )

            return pricing_data_scraped

    async def get_unit_pricing(self) -> UnitPricing:
        """Get the calculated unit pricing for CPU and memory with upcharge applied."""
        try:
            # Load CPU pricing from Redis
            cpu_price = await self._load_from_redis(
                REDIS_KEYS["single_cpu_price"], UnitPrice
            )

            # Load memory pricing from Redis
            memory_price = await self._load_from_redis(
                REDIS_KEYS["single_memory_price_gb"], UnitPrice
            )

            # Load upcharge from Redis
            upcharge = await self._load_from_redis(REDIS_KEYS["upcharge"], UpchargeData)

            # Apply upcharge to prices
            return self._apply_upcharge_to_prices(
                cpu_price, memory_price, upcharge.value
            )
        except ValueError:
            # If any key is missing, try to recalculate and return
            print("Unit pricing not found in Redis, recalculating...")

            # Get latest pricing data to calculate values
            pricing_data = await self.get_latest_pricing_data()

            if (
                not pricing_data.pricing_table
                or not pricing_data.pricing_table.pricing_rows
            ):
                raise ValueError(
                    "Unable to calculate unit pricing: pricing table not available"
                )

            # Calculate unit prices
            cpu_price, memory_price = self._calculate_cpu_memory_prices(
                pricing_data.pricing_table
            )

            # Get upcharge value
            upcharge_value = app_config.LAZYCLOUD_UPCHARGE

            # Apply upcharge to prices
            return self._apply_upcharge_to_prices(
                cpu_price, memory_price, upcharge_value
            )

    def _apply_upcharge_to_prices(
        self, cpu_price: UnitPrice, memory_price: UnitPrice, upcharge: float
    ) -> UnitPricing:
        """Apply upcharge to CPU and memory prices."""
        # Create new UnitPrice objects with upcharge applied
        cpu_with_upcharge = UnitPrice(
            price_sec=(
                cpu_price.price_sec * (1 + upcharge)
                if cpu_price.price_sec is not None
                else None
            ),
            price_hour=(
                cpu_price.price_hour * (1 + upcharge)
                if cpu_price.price_hour is not None
                else None
            ),
            price_month=(
                cpu_price.price_month * (1 + upcharge)
                if cpu_price.price_month is not None
                else None
            ),
        )

        memory_with_upcharge = UnitPrice(
            price_sec=(
                memory_price.price_sec * (1 + upcharge)
                if memory_price.price_sec is not None
                else None
            ),
            price_hour=(
                memory_price.price_hour * (1 + upcharge)
                if memory_price.price_hour is not None
                else None
            ),
            price_month=(
                memory_price.price_month * (1 + upcharge)
                if memory_price.price_month is not None
                else None
            ),
        )

        return UnitPricing(cpu=cpu_with_upcharge, memory_gb=memory_with_upcharge)

    async def get_platform_options(self) -> PlatformOptions:
        """Get available platform options."""
        pricing_data = await self.get_latest_pricing_data()

        if not pricing_data.markups or not pricing_data.markups.regions:
            raise ValueError("Required markups data not found")
        if (
            not pricing_data.pricing_table
            or not pricing_data.pricing_table.pricing_rows
        ):
            raise ValueError("Required pricing_table data (EWR base) not found")

        regions = [region.region for region in pricing_data.markups.regions]

        # Group CPU and RAM options from the EWR base table
        compute: Dict[int, List[int]] = {}
        for row in pricing_data.pricing_table.pricing_rows:
            # Only include performance machines if performance_only is true
            if self.performance_only and "performance" not in row.preset_group.lower():
                continue
            if row.cpus not in compute:
                compute[row.cpus] = []

            # Convert RAM to GB if in MB for consistent comparison/storage if needed
            ram_gb = row.ram
            if row.ram_units == "MB":
                ram_gb = row.ram / 1024  # Or handle as float if precision matters
            elif row.ram_units == "TB":
                ram_gb = row.ram * 1024

            if (
                ram_gb not in compute[row.cpus]
            ):  # Avoid duplicates if multiple entries for same CPU/RAM
                compute[row.cpus].append(int(ram_gb))

        # Sort RAM values for each CPU option
        for cpus in compute:
            compute[cpus] = self._sort_ram_values(compute[cpus])

        # Handle GPU options
        gpu: Dict[str, GPUInfo] = {}
        if pricing_data.gpu_pricing and pricing_data.gpu_pricing.gpu_rows:
            for row in pricing_data.gpu_pricing.gpu_rows:
                if row.price_hour is not None:
                    resource_name = GPU_NAME_MAP.get(row.model)
                    if resource_name:
                        gpu_regions = (
                            RESOURCE_MAP["gpus"]
                            .get(resource_name, {})
                            .get("regions", [])
                        )
                        gpu[resource_name] = GPUInfo(
                            regions=[r.value for r in gpu_regions],
                        )

        return PlatformOptions(regions=regions, compute=compute, gpu=gpu)


def format_price(price: Optional[float], unit: str = "") -> str:
    """Format price with currency symbol and unit."""
    return "N/A" if price is None else f"${price:.6f}{unit}"


def print_unit_price(price: UnitPrice, label: str) -> None:
    """Print unit price data in a formatted way."""
    print(f"\n=== {label} ===")
    print(f"  Per second: {format_price(price.price_sec, '/sec')}")
    print(f"  Per hour:   {format_price(price.price_hour, '/hr')}")
    print(f"  Per month:  {format_price(price.price_month, '/month')}")


def create_markup_df(markups):
    data = [(r.region, r.markup) for r in markups.regions]
    df = pd.DataFrame(data, columns=["Region", "Markup"])
    return df.sort_values("Region")


def create_gpu_df(gpu_rows):
    data = [(r.model, f"${r.price_hour:.4f}/hr") for r in gpu_rows]
    return pd.DataFrame(data, columns=["GPU Model", "Price"])


async def display_results(pricing_data: PricingData) -> None:
    if pricing_data.markups and pricing_data.markups.regions:
        print("\n=== REGION MARKUPS ===")
        print(create_markup_df(pricing_data.markups).to_string(index=False))

    if pricing_data.pricing_table and pricing_data.pricing_table.pricing_rows:
        print("\n=== EWR PRICING TABLE (Markup 1.0 Base) ===")
        try:
            df = pd.DataFrame(
                [row.model_dump() for row in pricing_data.pricing_table.pricing_rows]
            )
            for col in ["price_sec", "price_hour", "price_month"]:
                if col in df.columns:
                    df[col] = df[col].apply(
                        lambda x: format_price(x) if pd.notna(x) else "N/A"
                    )
            print(df.to_string(index=False))
        except Exception as e:
            print(f"Error displaying pricing table: {e}")

    if pricing_data.gpu_pricing and pricing_data.gpu_pricing.gpu_rows:
        print("\n=== GPU PRICING TABLE ===")
        print(create_gpu_df(pricing_data.gpu_pricing.gpu_rows).to_string(index=False))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--populate", action="store_true", help="Populate pricing data in Redis"
    )
    args = parser.parse_args()

    async def main() -> None:
        async with PlatformManager(performance_only=True) as manager:
            if args.populate:
                print("Populating pricing data in Redis...")
                await manager.update_pricing_data()

                print("\n" + "=" * 80)
                print(" " * 30 + "PRICING DATA IN REDIS")
                print("=" * 80)

                try:
                    # Load and display all data from Redis
                    markups = await manager._load_from_redis(
                        REDIS_KEYS["region_markups"], Markups
                    )
                    print("\n=== REGION MARKUPS ===")
                    print(create_markup_df(markups).to_string(index=False))

                    upcharge = await manager._load_from_redis(
                        REDIS_KEYS["upcharge"], UpchargeData
                    )
                    print(
                        f"\n=== UPCHARGE ===\n  Value: {upcharge.value:.4f} ({upcharge.value*100:.2f}%)"
                    )

                    gpu_pricing = await manager._load_from_redis(
                        REDIS_KEYS["gpu_pricing"], GPUPricingTable
                    )
                    print("\n=== GPU PRICING ===")
                    print(create_gpu_df(gpu_pricing.gpu_rows).to_string(index=False))

                    # Load and display calculated prices
                    cpu_price = await manager._load_from_redis(
                        REDIS_KEYS["single_cpu_price"], UnitPrice
                    )
                    print_unit_price(
                        cpu_price, "CALCULATED CPU PRICE (1 performance CPU)"
                    )

                    mem_price = await manager._load_from_redis(
                        REDIS_KEYS["single_memory_price_gb"], UnitPrice
                    )
                    print_unit_price(mem_price, "CALCULATED MEMORY PRICE (per 1GB)")

                except Exception as e:
                    print(f"Could not load prices from Redis: {e}")

                print("\n" + "=" * 80)
            else:
                pricing_data = await manager.scrape()

                print("\n" + "=" * 80)
                print(" " * 30 + "SCRAPED PRICING DATA")
                print("=" * 80)

                await display_results(pricing_data)

                if (
                    pricing_data.pricing_table
                    and pricing_data.pricing_table.pricing_rows
                ):
                    try:
                        cpu_price, mem_price = manager._calculate_cpu_memory_prices(
                            pricing_data.pricing_table
                        )
                        print_unit_price(
                            cpu_price, "CALCULATED CPU PRICE (1 performance CPU)"
                        )
                        print_unit_price(mem_price, "CALCULATED MEMORY PRICE (per 1GB)")
                    except ValueError as e:
                        print(
                            f"Could not calculate CPU/Memory prices from scraped data: {e}"
                        )

                print("\n" + "=" * 80)

    asyncio.run(main())
