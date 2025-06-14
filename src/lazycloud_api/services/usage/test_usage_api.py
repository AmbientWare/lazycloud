#!/usr/bin/env python3
"""Test script for the new UsageAPI class."""

import asyncio

from lazycloud_api.services.fly.api.usage import UsageAPI


async def test_monthly_usage():
    """Test the get_monthly_usage method."""
    usage_api = UsageAPI()

    print("=== Testing UsageAPI.get_monthly_usage() ===")

    try:
        print("Getting current month usage data...")

        summaries = await usage_api.get_monthly_usage()

        print(f"\n✅ SUCCESS! Found {len(summaries)} apps with usage data")

        if summaries:
            for summary in summaries:
                print(f"\n📱 App: {summary.app_name}")
                print(f"   Period: {summary.period_start} to {summary.period_end}")
                print(f"   Machine records: {len(summary.machine_records)}")
                print(f"   Volume records: {len(summary.volume_records)}")

                # Show machine records
                if summary.machine_records:
                    print("\n   🖥️  Machine Usage:")
                    for record in summary.machine_records:
                        day = record.day[:10]
                        print(
                            f"      {day}: {record.value:.0f} seconds ({record.hours:.2f} hours)"
                        )
                        print(f"         Config: {record.format_config()}")

                # Show volume records
                if summary.volume_records:
                    print("\n   💾 Volume Usage:")
                    for record in summary.volume_records:
                        day = record.day[:10]
                        print(f"      {day}: {record.value:.1f} GB-hours")
        else:
            print("❌ No usage data found for this period")

    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback

        traceback.print_exc()


async def main():
    """Run the test."""
    await test_monthly_usage()


if __name__ == "__main__":
    asyncio.run(main())
