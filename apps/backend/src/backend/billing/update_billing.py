import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from loguru import logger  # noqa: E402

from backend.billing.meters import setup_meters  # noqa: E402
from backend.billing.products import setup_products  # noqa: E402
from backend.config import app_config  # noqa: E402


async def update_billing() -> dict:
    """Update all Polar billing configuration (meters and products)"""
    organization_id = app_config.POLAR_ORGANIZATION_ID
    if not organization_id:
        logger.error("POLAR_ORGANIZATION_ID not configured")
        raise ValueError("POLAR_ORGANIZATION_ID must be set in configuration")

    access_token = app_config.POLAR_ACCESS_TOKEN
    if not access_token:
        logger.error("POLAR_ACCESS_TOKEN not configured")
        raise ValueError("POLAR_ACCESS_TOKEN must be set in configuration")

    logger.info(f"Updating billing configuration for organization: {organization_id}")
    logger.info("=" * 60)

    results = {"meters": {}, "products": {}}

    try:
        logger.info("Step 1/2: Setting up meters...")
        meters_result = await setup_meters(organization_id=organization_id)
        results["meters"] = meters_result

        print("\n" + "=" * 60)
        print("Polar Meter Setup Summary")
        print("=" * 60)
        print(f"Total meters:   {meters_result['total']}")
        print(f"Created:        {meters_result['created']}")
        print(f"Updated:        {meters_result['updated']}")
        print(f"Already exist:  {meters_result['skipped']}")
        print(f"Failed:         {meters_result['failed']}")
        print("=" * 60 + "\n")

        if meters_result["failed"] > 0:
            logger.warning(f"{meters_result['failed']} meter(s) failed to set up")

    except Exception as e:
        logger.error(f"Fatal error during meter setup: {e}")
        results["meters"]["error"] = str(e)
        raise

    try:
        logger.info("Step 2/2: Setting up products...")
        products_result = await setup_products(organization_id=organization_id)
        results["products"] = products_result

        print("\n" + "=" * 60)
        print("Polar Product Setup Summary")
        print("=" * 60)
        print(f"Total products: {products_result['total']}")
        print(f"Created:        {products_result['created']}")
        print(f"Updated:        {products_result['updated']}")
        print(f"Already exist:  {products_result['skipped']}")
        print(f"Archived:       {products_result.get('archived', 0)}")
        print(f"Failed:         {products_result['failed']}")
        print("=" * 60 + "\n")

        if products_result["failed"] > 0:
            logger.warning(f"{products_result['failed']} product(s) failed to set up")

    except Exception as e:
        logger.error(f"Fatal error during product setup: {e}")
        results["products"]["error"] = str(e)
        raise

    return results


def main():
    """CLI entry point for updating Polar billing configuration."""
    try:
        logger.info("Starting Polar billing configuration update...")
        result = asyncio.run(update_billing())

        print("\n" + "=" * 60)
        print("BILLING UPDATE COMPLETE")
        print("=" * 60)

        total_failed = result["meters"].get("failed", 0) + result["products"].get(
            "failed", 0
        )

        if total_failed > 0:
            logger.warning(f"Completed with {total_failed} failure(s)")
            sys.exit(1)

        logger.info("All billing configuration updated successfully!")
        sys.exit(0)

    except Exception as e:
        logger.error(f"Fatal error during billing update: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
