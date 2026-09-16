from shared.custom_domains import CustomDomain, CustomDomainErrorCode, CustomDomainPhase, DnsRecord
from shared.timestamps import to_utc, to_utc_or_none

from database.tables.custom_domains import CustomDomainTable


def custom_domain_from_table(row: CustomDomainTable) -> CustomDomain:
    return CustomDomain(
        id=row.id,
        user_id=row.user_id,
        hostname=row.hostname,
        phase=CustomDomainPhase(row.phase),
        provider_hostname_id=row.provider_hostname_id,
        required_records=tuple(DnsRecord.model_validate(record) for record in row.required_records),
        error_code=CustomDomainErrorCode(row.error_code) if row.error_code is not None else None,
        error_message=row.error_message,
        verified_at=to_utc_or_none(row.verified_at),
        last_checked_at=to_utc_or_none(row.last_checked_at),
        created_at=to_utc(row.created_at),
        updated_at=to_utc(row.updated_at),
        deleted_at=to_utc_or_none(row.deleted_at),
    )


def write_custom_domain(row: CustomDomainTable, domain: CustomDomain) -> None:
    row.hostname = domain.hostname
    row.phase = domain.phase.value
    row.provider_hostname_id = domain.provider_hostname_id
    row.required_records = [
        {"type": record.type, "name": record.name, "value": record.value}
        for record in domain.required_records
    ]
    row.error_code = domain.error_code.value if domain.error_code is not None else None
    row.error_message = domain.error_message
    row.verified_at = domain.verified_at
    row.last_checked_at = domain.last_checked_at
    row.deleted_at = domain.deleted_at
    row.updated_at = domain.updated_at
