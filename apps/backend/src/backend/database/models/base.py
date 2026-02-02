import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel as PydanticBaseModel
from pydantic.functional_validators import BeforeValidator
from sqlalchemy import orm

Base = orm.declarative_base()


# Custom type that automatically converts UUID to string
def _uuid_to_str(v):
    if isinstance(v, uuid.UUID):
        return str(v)
    return v


UUIDStr = Annotated[str, BeforeValidator(_uuid_to_str)]


class BaseDbModel(PydanticBaseModel):
    """Base class for all Pydantic models with an id"""

    id: UUIDStr
    created_at: datetime
    updated_at: datetime
