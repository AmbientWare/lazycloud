from __future__ import annotations

import base64
import hashlib
import hmac

from shared.contracts import ContractModel


class PayloadSignature(ContractModel):
    key: str
    timestamp: int


def sign_payload(
    payload: bytes,
    secret_key: str,
    *,
    timestamp: int,
) -> PayloadSignature:
    encoded_payload = base64.b64encode(payload).decode("ascii")
    data_to_sign = f"{encoded_payload}:{timestamp}".encode()
    digest = hmac.new(secret_key.encode("utf-8"), data_to_sign, hashlib.sha256).hexdigest()
    return PayloadSignature(key=digest, timestamp=timestamp)


def verify_payload_signature(
    payload: bytes,
    secret_key: str,
    signature: PayloadSignature,
) -> bool:
    expected = sign_payload(payload, secret_key, timestamp=signature.timestamp)
    return hmac.compare_digest(expected.key, signature.key)
