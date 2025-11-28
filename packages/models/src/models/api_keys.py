from enum import IntEnum


class ApiKeyExpirationMinutes(IntEnum):
    """The expiration time for a api key in minutes"""

    NEVER = 0
    THIRTY_MINUTES = 30
    ONE_HOUR = 60
    THREE_HOURS = 180


class ApiKeyExpirationDays(IntEnum):
    """The expiration time for a api key in days"""

    NEVER = 0
    ONE_DAY = 1
    THIRTY_DAYS = 30
    ONE_HUNDRED_DAYS = 100
    THREE_HUNDRED_SIXTY_FIVE_DAYS = 365
