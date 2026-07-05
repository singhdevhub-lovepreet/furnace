from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class VaultError(RuntimeError):
    pass


@dataclass(slots=True)
class KeyVault:
    master_key: bytes
    version: int = 1

    @classmethod
    def from_base64_key(cls, encoded_key: str) -> KeyVault:
        try:
            master_key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, TypeError) as exc:
            raise VaultError("master encryption key must be base64-encoded") from exc
        if len(master_key) != 32:
            raise VaultError("master encryption key must decode to exactly 32 bytes")
        return cls(master_key=master_key)

    def encrypt(self, plaintext: str) -> bytes:
        nonce = secrets.token_bytes(12)
        aesgcm = AESGCM(self.master_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return bytes([self.version]) + nonce + ciphertext

    def decrypt(self, blob: bytes) -> str:
        if len(blob) < 13:
            raise VaultError("encrypted blob is too short")
        version = blob[0]
        if version != self.version:
            raise VaultError(f"unsupported vault blob version {version}")
        nonce = blob[1:13]
        ciphertext = blob[13:]
        aesgcm = AESGCM(self.master_key)
        try:
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        except InvalidTag as exc:
            raise VaultError("encrypted blob failed authentication") from exc
        return plaintext.decode("utf-8")
