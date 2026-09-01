from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError
from shared.custom_domains import CustomDomain


def test_a_persisted_custom_domain_must_name_one_exact_hostname() -> None:
    with pytest.raises(ValidationError, match="domain must be an exact hostname"):
        CustomDomain(
            id=str(uuid4()),
            user_id=str(uuid4()),
            hostname="*.acme.com",
        )
