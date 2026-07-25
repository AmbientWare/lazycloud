from __future__ import annotations

import base64
import binascii
import secrets
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SECRET_VALUE_PREFIX = "wssec:v1:"
_KEY_LENGTH = 32
_NONCE_LENGTH = 12
_HKDF_INFO = b"workspace-secret-encryption:v1"
_AAD_PREFIX = b"workspace-secret:v1"


class SecretEncryptionError(ValueError):
    pass


class SecretDecryptionError(ValueError):
    pass


class WorkspaceSecretMaterial(Protocol):
    id: str
    signing_key: str


@dataclass(frozen=True, slots=True)
class WorkspaceSecretCipher:
    workspace_id: str
    signing_key: str

    @classmethod
    def from_workspace(cls, workspace: WorkspaceSecretMaterial) -> WorkspaceSecretCipher:
        return cls(workspace_id=workspace.id, signing_key=workspace.signing_key)

    def encrypt(self, name: str, plaintext: str) -> str:
        nonce = secrets.token_bytes(_NONCE_LENGTH)
        ciphertext = AESGCM(self._key()).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            _associated_data(self.workspace_id, name),
        )
        payload = base64.b64encode(nonce + ciphertext).decode("ascii")
        return f"{SECRET_VALUE_PREFIX}{payload}"

    def decrypt(self, name: str, ciphertext: str) -> str:
        if not is_encrypted_secret_value(ciphertext):
            msg = "secret ciphertext does not use the current encrypted format"
            raise SecretDecryptionError(msg)
        token = ciphertext.removeprefix(SECRET_VALUE_PREFIX)
        try:
            payload = base64.b64decode(token.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            msg = "secret ciphertext is not valid base64"
            raise SecretDecryptionError(msg) from exc
        if len(payload) <= _NONCE_LENGTH:
            msg = "secret ciphertext is too short"
            raise SecretDecryptionError(msg)
        nonce = payload[:_NONCE_LENGTH]
        encrypted_payload = payload[_NONCE_LENGTH:]
        try:
            plaintext = AESGCM(self._key()).decrypt(
                nonce,
                encrypted_payload,
                _associated_data(self.workspace_id, name),
            )
        except InvalidTag as exc:
            msg = "secret ciphertext failed authentication"
            raise SecretDecryptionError(msg) from exc
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "secret plaintext is not valid utf-8"
            raise SecretDecryptionError(msg) from exc

    def _key(self) -> bytes:
        if not self.workspace_id or not self.signing_key:
            msg = "workspace secret encryption requires a workspace id and signing key"
            raise SecretEncryptionError(msg)
        return HKDF(
            algorithm=hashes.SHA256(),
            length=_KEY_LENGTH,
            salt=self.workspace_id.encode("utf-8"),
            info=_HKDF_INFO,
        ).derive(self.signing_key.encode("utf-8"))


def is_encrypted_secret_value(value: str) -> bool:
    return value.startswith(SECRET_VALUE_PREFIX)


def _associated_data(workspace_id: str, name: str) -> bytes:
    return b"\0".join(
        (
            _AAD_PREFIX,
            workspace_id.encode("utf-8"),
            name.encode("utf-8"),
        )
    )
