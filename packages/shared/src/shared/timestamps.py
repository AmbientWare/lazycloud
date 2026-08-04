from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    # A tzinfo whose utcoffset() is None leaves the value naive by Python's own rule, and
    # astimezone() would then reinterpret it as local time rather than UTC.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_utc_or_none(value: datetime | None) -> datetime | None:
    return to_utc(value) if value is not None else None


__all__ = ["to_utc", "to_utc_or_none", "utc_now"]
