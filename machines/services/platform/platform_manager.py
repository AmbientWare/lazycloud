import asyncio
from typing import Dict, List, Optional, Any, Sequence, cast
import re

import aiohttp
from bs4 import BeautifulSoup, Tag
from bs4.element import PageElement, ResultSet
import pandas as pd
from pydantic import ValidationError
from redis.asyncio import Redis

from machines.config import app_config
from machines.services.platform.schemas import (
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
from machines.services.fly.schemas import RESOURCE_MAP, FlyRegion

# Constants
FLY_PRICING_URL = "https://fly.io/docs/about/pricing/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
EWR_TABLE_ID = "started-machines-pricing-matrix-ewr"

# Redis keys
REDIS_KEYS = {
    "region_markups": "pricing:region_markups",
    "pricing_table": "pricing:pricing_table",
    "gpu_pricing": "pricing:gpu_pricing",
}

# GPU name mapping between pricing table and resource map
GPU_NAME_MAP = {
    "A10": "a10",
    "L40S": "l40s",
    "A100 40G PCIe": "a100-40gb",
    "A100 80G SXM": "a100-80gb",
}


class PlatformManager:
    def __init__(
        self,
        url: str = FLY_PRICING_URL,
        user_agent: str = USER_AGENT,
        performance_only: bool = True,
    ):
        """Initialize the platform manager.

        Args:
            url: The URL to scrape pricing data from
            user_agent: User agent string for HTTP requests
            performance_only: Whether to only extract performance machine pricing
        """
        self.url = url
        self.headers = {"User-Agent": user_agent}
        self.performance_only = performance_only

        # Internal state
        self.soup: Optional[BeautifulSoup] = None
        self._region_markups: Optional[Markups] = None
        self._pricing_table: Optional[PricingTable] = None
        self._gpu_pricing: Optional[GPUPricingTable] = None

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
                        markup=float(value) * (1 + app_config.LAZYCLOUD_UPCHARGE),
                    )
                    for key, value in markups_raw
                    if key in valid_regions
                ]
                return Markups(regions=regions)
            except (ValidationError, ValueError) as e:
                print(f"Error parsing markups: {e}")

        raise ValueError("Could not find region markups data")

    async def _extract_pricing_table(self) -> PricingTable:
        """Extract machine pricing data from HTML."""
        if not self.soup:
            raise ValueError("BeautifulSoup not initialized")

        pricing_rows = []
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
                    pricing_rows.append(pricing_row)
            except (ValueError, ValidationError) as e:
                print(f"Error parsing row: {e}")
                continue

        if not pricing_rows:
            raise ValueError("No valid pricing data extracted")

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

            machine_type = first_cell.get_text(strip=True)
            if self.performance_only and "performance" not in machine_type.lower():
                return None

            cpu_count = cells[1].get_text(strip=True).split()[0]
            ram_text = cells[2].get_text(strip=True)

            return self._create_pricing_row(
                machine_type,
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
            and (
                not self.performance_only
                or "performance" in current_machine_type.lower()
            )
        ):
            if len(cells) < 4:
                return None

            ram_text = cells[-4].get_text(strip=True)
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
            price_sec_float = float(price_sec.replace("$", "")) if price_sec else None
            price_hour_float = (
                float(price_hour.replace("$", "")) if price_hour else None
            )
            price_month_float = (
                float(price_month.replace("$", "")) if price_month else None
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
            return PricingData(
                markups=Markups(regions=[]),
                pricing_table=PricingTable(pricing_rows=[]),
                gpu_pricing=GPUPricingTable(gpu_rows=[]),
            )

        self._region_markups = await self._extract_region_markups()
        self._pricing_table = await self._extract_pricing_table()
        self._gpu_pricing = await self._extract_gpu_pricing()

        return PricingData(
            markups=self._region_markups,
            pricing_table=self._pricing_table,
            gpu_pricing=self._gpu_pricing,
        )

    async def update_pricing_data(self) -> None:
        """Update all pricing data in Redis."""
        pricing_data = await self.scrape()

        await asyncio.gather(
            self._save_to_redis(REDIS_KEYS["region_markups"], pricing_data.markups),
            self._save_to_redis(
                REDIS_KEYS["pricing_table"], pricing_data.pricing_table
            ),
            self._save_to_redis(REDIS_KEYS["gpu_pricing"], pricing_data.gpu_pricing),
        )

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
            return await self.scrape()

    async def get_platform_options(self) -> PlatformOptions:
        """Get available platform options."""
        pricing_data = await self.get_latest_pricing_data()

        if not pricing_data.markups or not pricing_data.pricing_table:
            raise ValueError("Required pricing data not found")

        regions = [region.region for region in pricing_data.markups.regions]

        # Group CPU and RAM options
        compute: Dict[int, List[int]] = {}
        for row in pricing_data.pricing_table.pricing_rows:
            if row.cpus not in compute:
                compute[row.cpus] = []
            compute[row.cpus].append(row.ram)

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
                        gpu[resource_name] = GPUInfo(
                            price=row.price_hour,
                            regions=[
                                r.value
                                for r in RESOURCE_MAP["gpus"]
                                .get(resource_name, {})
                                .get("regions", [])
                            ],
                        )

        return PlatformOptions(regions=regions, compute=compute, gpu=gpu)


async def display_results(pricing_data: PricingData) -> None:
    """Display pricing data in a formatted way."""
    # Display region markups
    if pricing_data.markups and pricing_data.markups.regions:
        print("\n--- Region Markups ---")
        for region in pricing_data.markups.regions:
            print(f"  {region.region}: {region.markup}")

    # Display pricing table
    if pricing_data.pricing_table and pricing_data.pricing_table.pricing_rows:
        print("\n--- EWR Pricing Table ---")
        try:
            df = pd.DataFrame(
                [row.model_dump() for row in pricing_data.pricing_table.pricing_rows]
            )
            print(df.to_string())
        except Exception as e:
            print(f"Error displaying pricing table: {e}")

    # Display GPU pricing
    if pricing_data.gpu_pricing and pricing_data.gpu_pricing.gpu_rows:
        print("\n--- GPU Pricing Table ---")
        try:
            df = pd.DataFrame(
                [row.model_dump() for row in pricing_data.gpu_pricing.gpu_rows]
            )
            print(df.to_string())
        except Exception as e:
            print(f"Error displaying GPU pricing: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--populate", action="store_true", help="Populate pricing data in Redis"
    )
    args = parser.parse_args()

    async def main() -> None:
        async with PlatformManager() as manager:
            pricing_data = await manager.scrape()
            await display_results(pricing_data)

            if args.populate:
                await manager.update_pricing_data()

    asyncio.run(main())
