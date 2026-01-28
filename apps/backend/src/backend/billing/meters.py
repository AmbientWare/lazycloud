from dataclasses import dataclass

import polar_sdk
from loguru import logger
from models.billing import METER_METADATA_FIELDS, USAGE_EVENT_NAME, MeterNames
from polar_sdk.models import Filter, FilterClause, PropertyAggregation

from backend.services import get_polar_service


@dataclass
class MeterDefinition:
    """Complete meter definition for LazyCloud usage tracking."""

    name: str
    filter: Filter
    aggregation: PropertyAggregation


METER_DEFINITIONS = [
    MeterDefinition(
        name=MeterNames.CPU_USAGE,
        filter=Filter(
            conjunction=polar_sdk.FilterConjunction.OR,
            clauses=[
                FilterClause(
                    property="name",
                    operator=polar_sdk.FilterOperator.EQ,
                    value=USAGE_EVENT_NAME,
                )
            ],
        ),
        aggregation=PropertyAggregation(
            func=polar_sdk.Func.SUM,
            property=METER_METADATA_FIELDS[MeterNames.CPU_USAGE],
        ),
    ),
    MeterDefinition(
        name=MeterNames.MEMORY_USAGE,
        filter=Filter(
            conjunction=polar_sdk.FilterConjunction.OR,
            clauses=[
                FilterClause(
                    property="name",
                    operator=polar_sdk.FilterOperator.EQ,
                    value=USAGE_EVENT_NAME,
                )
            ],
        ),
        aggregation=PropertyAggregation(
            func=polar_sdk.Func.SUM,
            property=METER_METADATA_FIELDS[MeterNames.MEMORY_USAGE],
        ),
    ),
    MeterDefinition(
        name=MeterNames.BUILD_MINUTES,
        filter=Filter(
            conjunction=polar_sdk.FilterConjunction.OR,
            clauses=[
                FilterClause(
                    property="name",
                    operator=polar_sdk.FilterOperator.EQ,
                    value=USAGE_EVENT_NAME,
                )
            ],
        ),
        aggregation=PropertyAggregation(
            func=polar_sdk.Func.SUM,
            property=METER_METADATA_FIELDS[MeterNames.BUILD_MINUTES],
        ),
    ),
    MeterDefinition(
        name=MeterNames.STORAGE_USAGE,
        filter=Filter(
            conjunction=polar_sdk.FilterConjunction.OR,
            clauses=[
                FilterClause(
                    property="name",
                    operator=polar_sdk.FilterOperator.EQ,
                    value=USAGE_EVENT_NAME,
                )
            ],
        ),
        aggregation=PropertyAggregation(
            func=polar_sdk.Func.SUM,
            property=METER_METADATA_FIELDS[MeterNames.STORAGE_USAGE],
        ),
    ),
]


async def setup_meters(organization_id: str) -> dict:
    """Set up Polar meters for LazyCloud usage tracking"""
    logger.info(f"Setting up Polar meters for organization: {organization_id}")

    # Initialize Polar service
    polar = get_polar_service()

    if not polar.enabled:
        logger.error("Polar service is not enabled")
        raise RuntimeError("Polar service is not enabled")

    created = 0
    updated = 0
    skipped = 0
    failed = 0

    # Process each meter definition
    for meter_def in METER_DEFINITIONS:
        name = meter_def.name

        try:
            # Check if meter already exists by name
            existing_meter = await polar.meters.get_meter_by_name(
                name=name, organization_id=organization_id
            )

            if existing_meter:
                # Meter exists - check if it needs updating
                needs_update = False
                update_fields = {}

                # Compare filter
                if existing_meter.filter_ != meter_def.filter:
                    needs_update = True
                    update_fields["filter_"] = meter_def.filter
                    logger.info(f"Meter '{name}' has different filter")

                # Compare aggregation
                if existing_meter.aggregation != meter_def.aggregation:
                    needs_update = True
                    update_fields["aggregation"] = meter_def.aggregation
                    logger.info(f"Meter '{name}' has different aggregation")

                if needs_update:
                    logger.info(f"Updating meter '{name}' with new configuration...")
                    result = await polar.meters.update_meter(
                        meter_id=existing_meter.id, **update_fields
                    )
                    if result:
                        logger.info(
                            f"Updated meter '{name}' with ID: {existing_meter.id}"
                        )
                        updated += 1
                    else:
                        logger.error(f"Failed to update meter '{name}'")
                        failed += 1
                else:
                    logger.info(
                        f"Meter '{name}' already exists and is up-to-date with ID: {existing_meter.id}"
                    )
                    skipped += 1
                continue

            # Create the meter
            meter = await polar.meters.create_meter(
                organization_id=organization_id,
                name=name,
                filter_=meter_def.filter,
                aggregation=meter_def.aggregation,
            )

            if meter:
                logger.info(f"Created meter '{name}' with ID: {meter.id}")
                created += 1
            else:
                logger.error(f"Failed to create meter '{name}'")
                failed += 1

        except Exception as e:
            logger.error(f"Error processing meter '{name}': {e}")
            failed += 1

    # Summary
    logger.info(
        f"Meter setup complete: {created} created, {updated} updated, "
        f"{skipped} skipped, {failed} failed"
    )

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "total": len(METER_DEFINITIONS),
    }
