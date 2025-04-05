import aiohttp
from bs4 import BeautifulSoup, Tag
import re
import pandas as pd
from pprint import pprint
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, field_validator, ValidationError
from redis.asyncio import Redis
import asyncio
import nest_asyncio

from machines.config import app_config


# This is needed to run asyncio in the main thread
nest_asyncio.apply()

FLY_PRICING_URL = "https://fly.io/docs/about/pricing/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
EWR_TABLE_ID = "started-machines-pricing-matrix-ewr"


class Region(BaseModel):
    region: str
    markup: float


class Markups(BaseModel):
    regions: List[Region]


class PricingRow(BaseModel):
    preset_group: str
    cpus: str
    ram: str
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


# --- Scraper Class ---
class PricingManager:
    def __init__(self, url: str = FLY_PRICING_URL, user_agent: str = USER_AGENT):
        self.url = url
        self.headers = {"User-Agent": user_agent}
        self.soup: Optional[BeautifulSoup] = None
        self._region_markups: Optional[Markups] = None
        self._pricing_table: Optional[PricingTable] = None
        self._redis_client: Optional[Redis] = None
        self._region_markup_key = "pricing:region_markups"
        self._pricing_table_key = "pricing:pricing_table"

    async def initialize(self):
        """Initialize async resources."""
        self._redis_client = Redis.from_url(app_config.REDIS_URL)
        await self._redis_client.ping()  # Test connection

    async def cleanup(self):
        """Cleanup async resources."""
        if self._redis_client:
            await self._redis_client.aclose()

    async def add_region_markups_to_redis(self):
        """Add region markups to Redis."""
        if not self._redis_client:
            raise RuntimeError("Redis client not initialized. Call initialize() first.")

        if not self._region_markups:
            raise ValueError("No region markups found. Call scrape() first.")

        await self._redis_client.set(
            self._region_markup_key, self._region_markups.model_dump_json()
        )

    async def add_pricing_table_to_redis(self):
        """Add pricing table to Redis."""
        if not self._redis_client:
            raise RuntimeError("Redis client not initialized. Call initialize() first.")

        if not self._pricing_table:
            raise ValueError("No pricing table found. Call scrape() first.")

        await self._redis_client.set(
            self._pricing_table_key, self._pricing_table.model_dump_json()
        )

    async def get_region_markups_from_redis(self) -> Markups:
        """Get region markups from Redis."""
        if not self._redis_client:
            raise RuntimeError("Redis client not initialized. Call initialize() first.")

        redis_data = await self._redis_client.get(self._region_markup_key)
        if redis_data:
            return Markups.model_validate_json(redis_data.decode("utf-8"))

        raise ValueError("No region markups found in Redis.")

    async def get_pricing_table_from_redis(self) -> PricingTable:
        """Get pricing table from Redis."""
        if not self._redis_client:
            raise RuntimeError("Redis client not initialized. Call initialize() first.")

        redis_data = await self._redis_client.get(self._pricing_table_key)
        if redis_data:
            return PricingTable.model_validate_json(redis_data.decode("utf-8"))

        raise ValueError("No pricing table found in Redis.")

    async def _fetch_html(self) -> bool:
        """Fetches HTML content and populates self.soup."""
        print(f"Fetching data from {self.url}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url, headers=self.headers) as response:
                    response.raise_for_status()
                    html_content = await response.text()
                    print("Successfully fetched page content.")
                    # Run BeautifulSoup parsing in a thread pool since it's CPU-bound
                    self.soup = await asyncio.to_thread(
                        BeautifulSoup, html_content, "html.parser"
                    )
                    return True
        except aiohttp.ClientError as e:
            print(f"Error fetching URL {self.url}: {e}")
            self.soup = None
            return False

    async def _extract_region_markups(self) -> List[Region]:
        """Extracts region markups from the script tag in the soup."""
        if not self.soup:
            print("Cannot extract region markups, soup is not loaded.")
            return []

        print("Extracting regionMarkups...")
        # Run BeautifulSoup operations in a thread pool
        scripts = await asyncio.to_thread(
            self.soup.find_all, "script", type="text/javascript"
        )
        for script in scripts:
            if isinstance(script, Tag):
                script_content = script.string
            else:
                continue  # Skip non-Tag elements like NavigableString

            if script_content and "const regionMarkups = {" in script_content:
                match = re.search(
                    r"const\s+regionMarkups\s*=\s*(\{.*?\});",
                    script_content,
                    re.DOTALL | re.MULTILINE,
                )
                if match:
                    markup_string = match.group(1)
                    try:
                        markups_raw = re.findall(r'"(\w+)":\s*([\d.]+)', markup_string)
                        # Convert dict items to Region objects
                        regions = [
                            Region(region=key, markup=float(value))
                            for key, value in markups_raw
                        ]
                        print("Successfully extracted and parsed regionMarkups.")
                        return regions

                    except (ValidationError, ValueError, TypeError) as e:
                        print(f"Error parsing/validating regionMarkups object: {e}")
                        print(f"Attempted to parse string segment: {markup_string}")

                else:
                    print(
                        "Found script containing 'regionMarkups' but regex failed to match the object."
                    )
                    print(
                        f"Script content sample: {script_content[:200]}..."
                    )  # Log sample

        print("Could not find or parse the regionMarkups script.")
        return []

    async def _extract_pricing_table(self) -> PricingTable:
        """Extracts pricing data from the specified table ID into Pydantic models."""
        if not self.soup:
            print("Cannot extract EWR pricing, soup is not loaded.")
            return PricingTable(pricing_rows=[])

        print(f"Extracting pricing table...")
        pricing_table_data_models: PricingTable = PricingTable(pricing_rows=[])

        # Run BeautifulSoup operations in a thread pool
        table_div = await asyncio.to_thread(self.soup.find, "div", id=EWR_TABLE_ID)

        if not table_div:
            print(f"Could not find the container div with id '{EWR_TABLE_ID}'")
            return pricing_table_data_models

        assert isinstance(table_div, Tag)

        html_table = await asyncio.to_thread(table_div.find, "table")
        if not html_table:
            print(f"Found div with id '{EWR_TABLE_ID}', but no table inside.")
            return pricing_table_data_models

        assert isinstance(html_table, Tag)

        tbody = await asyncio.to_thread(html_table.find, "tbody")
        if not tbody:
            print(f"Could not find tbody in table '{EWR_TABLE_ID}'")
            return pricing_table_data_models
        assert isinstance(tbody, Tag)

        rows = await asyncio.to_thread(tbody.find_all, "tr")
        if not rows:
            print(f"No rows found in tbody for table '{EWR_TABLE_ID}'")
            return pricing_table_data_models

        current_machine_type: Optional[str] = None
        current_cpus: Optional[str] = None

        for i, row in enumerate(rows):
            if not isinstance(row, Tag):
                continue

            cells = await asyncio.to_thread(row.find_all, ["th", "td"])
            if not cells:
                continue  # Skip empty rows

            first_cell = cells[0]
            if not isinstance(first_cell, Tag):
                continue

            raw_row_data: Dict[str, Any] = {}
            try:
                if first_cell.name == "th" and first_cell.has_attr("rowspan"):
                    current_machine_type = await asyncio.to_thread(
                        first_cell.get_text, strip=True
                    )
                    if len(cells) >= 6:
                        current_cpus = await asyncio.to_thread(
                            cells[1].get_text, strip=True
                        )
                        raw_row_data = {
                            "preset_group": current_machine_type,
                            "cpus": current_cpus,
                            "ram": await asyncio.to_thread(
                                cells[2].get_text, strip=True
                            ),
                            "price_sec": await asyncio.to_thread(
                                cells[3].get_text, strip=True
                            ),
                            "price_hour": await asyncio.to_thread(
                                cells[4].get_text, strip=True
                            ),
                            "price_month": await asyncio.to_thread(
                                cells[5].get_text, strip=True
                            ),
                        }
                    else:
                        print(
                            f"Warning: Row {i+1}: Machine type row has unexpected cell count: {[await asyncio.to_thread(c.get_text, strip=True) for c in cells]}"
                        )
                        current_cpus = None
                        continue

                elif first_cell.name == "td" and current_machine_type and current_cpus:
                    if len(cells) >= 4:
                        raw_row_data = {
                            "preset_group": current_machine_type,
                            "cpus": current_cpus,
                            "ram": await asyncio.to_thread(
                                cells[-4].get_text, strip=True
                            ),
                            "price_sec": await asyncio.to_thread(
                                cells[-3].get_text, strip=True
                            ),
                            "price_hour": await asyncio.to_thread(
                                cells[-2].get_text, strip=True
                            ),
                            "price_month": await asyncio.to_thread(
                                cells[-1].get_text, strip=True
                            ),
                        }
                    else:
                        print(
                            f"Warning: Row {i+1}: Data row has unexpected cell count: {[await asyncio.to_thread(c.get_text, strip=True) for c in cells]}"
                        )
                        continue
                else:
                    continue

                if raw_row_data:
                    validated_row = PricingRow(**raw_row_data)
                    pricing_table_data_models.pricing_rows.append(validated_row)

            except ValidationError as e:
                print(f"Pydantic Validation Error on row {i+1}: {e}")
                print(f"Raw data: {raw_row_data}")
            except Exception as e:
                print(f"Error processing row {i+1}: {e}")
                print(
                    f"Cells: {[await asyncio.to_thread(c.get_text, strip=True) for c in cells]}"
                )

        if pricing_table_data_models:
            print(
                f"Successfully extracted and validated {len(pricing_table_data_models.pricing_rows)} rows from the EWR table."
            )
        else:
            print(
                "No valid data extracted from the EWR table, check selectors or page structure."
            )

        return pricing_table_data_models

    async def scrape(self) -> PricingData:
        """Performs the complete scraping process."""
        if not await self._fetch_html() or not self.soup:
            return PricingData(
                markups=Markups(regions=[]), pricing_table=PricingTable(pricing_rows=[])
            )

        region_markups = await self._extract_region_markups()
        self.region_markups = region_markups

        pricing_table = await self._extract_pricing_table()
        self.pricing_table = pricing_table

        pricing_data = PricingData(
            markups=Markups(regions=region_markups), pricing_table=pricing_table
        )

        return pricing_data

    async def update_pricing_data(self) -> None:
        """Updates pricing data in Redis."""
        _ = await self.scrape()
        await self.add_region_markups_to_redis()
        await self.add_pricing_table_to_redis()

    async def get_latest_pricing_data(self) -> PricingData:
        """Gets the latest pricing data from Redis."""
        try:
            pricing_table = await self.get_pricing_table_from_redis()
            markups = await self.get_region_markups_from_redis()
            return PricingData(markups=markups, pricing_table=pricing_table)

        except ValueError:
            # If data is not in Redis, fetch it fresh
            return await self.scrape()


# --- Helper function to display the scraped results ---
async def display_results(pricing_data: PricingData):
    """Displays the scraped results."""
    # Check if the regions list within markups is not empty
    if pricing_data.markups and pricing_data.markups.regions:
        print("\n--- Region Markups ---")
        # Display the list of Region objects nicely
        for region_info in pricing_data.markups.regions:
            print(f"  {region_info.region}: {region_info.markup}")
    else:
        print("\nNo region markups found or extracted.")

    if pricing_data:
        print("\n--- EWR Pricing Table ---")
        try:
            # Convert Pydantic models to dicts for DataFrame creation
            pricing_list_of_dicts = [
                row.model_dump() for row in pricing_data.pricing_table.pricing_rows
            ]
            df = pd.DataFrame(pricing_list_of_dicts)
            # Prices are already floats due to Pydantic validation
            print(df.to_string())

        except ImportError:
            print("Pandas not installed. Printing raw list:")
            pprint(pricing_data)

        except Exception as e:
            print(f"Error creating/processing DataFrame: {e}")
            print("Printing raw list:")
            pprint(pricing_data)
    else:
        print("\nNo EWR pricing data found or extracted.")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--populate",
        action="store_true",
        help="Populate pricing data in Redis",
    )
    args = parser.parse_args()

    async def main():
        scraper = PricingManager()
        try:
            await scraper.initialize()
            pricing_data = await scraper.scrape()
            await display_results(pricing_data)

            if args.populate:
                await scraper.update_pricing_data()

        finally:
            await scraper.cleanup()

    asyncio.run(main())
