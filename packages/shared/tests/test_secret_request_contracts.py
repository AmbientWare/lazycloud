from __future__ import annotations

from pydantic import SecretStr
from shared.http.users import PasswordChangeRequest, SessionCreateRequest, UserCreateRequest

_SECRET = "a-password-somebody-chose"


def test_a_request_carrying_a_password_serializes_the_password() -> None:
    """A masked secret on the wire is a password everybody knows.

    `SecretStr` dumps as `**********`, so a client that serializes one of these
    creates the account with that string as its password and then signs in with the
    same string — which looks like it works, and hands the account to anyone.
    """
    created = UserCreateRequest(username="someone", password=SecretStr(_SECRET))
    signed_in = SessionCreateRequest(username="someone", password=SecretStr(_SECRET))
    changed = PasswordChangeRequest(
        current_password=SecretStr(_SECRET),
        new_password=SecretStr("something-else-entirely"),
    )

    assert created.model_dump(mode="json")["password"] == _SECRET
    assert signed_in.model_dump(mode="json")["password"] == _SECRET
    assert changed.model_dump(mode="json")["current_password"] == _SECRET
    assert changed.model_dump(mode="json")["new_password"] == "something-else-entirely"
    assert repr(created.password) == "SecretStr('**********')"
