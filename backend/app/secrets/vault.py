"""Envelope encryption (spec section 5).

Blob layout: key_version(4) | wrap_nonce(12) | wrapped_dek(48) | nonce(12) | ciphertext+tag
A random 256-bit DEK encrypts each secret with AES-256-GCM; the DEK is wrapped by the master key.
AAD (the owning object id) binds a blob to its row, so copying it elsewhere fails to decrypt.
"""
import base64
import os
import struct
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings
from app.errors import AppError

_HEADER = 4 + 12 + 48 + 12


class Vault:
    def __init__(self, keys: dict[int, bytes], current_version: int):
        for v, k in keys.items():
            if len(k) != 32:
                raise ValueError(f"master key v{v} must be 32 bytes")
        self._keys, self.current_version = keys, current_version

    @classmethod
    def from_settings(cls) -> "Vault":
        s = get_settings()
        raw = s.master_key
        if not raw and s.master_key_file:
            with open(s.master_key_file, encoding="ascii") as f:
                raw = f.read().strip()
        if not raw:
            raise AppError("UPSTREAM_ERROR", "Master key is not configured")
        return cls({1: base64.b64decode(raw)}, 1)

    @staticmethod
    def generate_key() -> str:
        return base64.b64encode(os.urandom(32)).decode()

    def seal(self, plaintext: bytes, aad: uuid.UUID) -> bytes:
        dek, nonce, wrap_nonce = os.urandom(32), os.urandom(12), os.urandom(12)
        a = aad.bytes
        wrapped = AESGCM(self._keys[self.current_version]).encrypt(wrap_nonce, dek, a)
        ct = AESGCM(dek).encrypt(nonce, plaintext, a)
        return struct.pack(">I", self.current_version) + wrap_nonce + wrapped + nonce + ct

    def open(self, blob: bytes, aad: uuid.UUID) -> bytes:
        try:
            if len(blob) < _HEADER + 16:
                raise ValueError("short blob")
            (version,) = struct.unpack(">I", blob[:4])
            wrap_nonce, wrapped = blob[4:16], blob[16:64]
            nonce, ct = blob[64:76], blob[76:]
            a = aad.bytes
            dek = AESGCM(self._keys[version]).decrypt(wrap_nonce, wrapped, a)
            return AESGCM(dek).decrypt(nonce, ct, a)
        except (InvalidTag, ValueError, KeyError):
            raise AppError("UPSTREAM_ERROR", "Secret could not be decrypted") from None

    def rewrap(self, blob: bytes, aad: uuid.UUID) -> bytes:
        """Re-wrap the DEK under the current master key without touching the ciphertext (rotation)."""
        (version,) = struct.unpack(">I", blob[:4])
        if version == self.current_version:
            return blob
        a = aad.bytes
        try:
            dek = AESGCM(self._keys[version]).decrypt(blob[4:16], blob[16:64], a)
        except (InvalidTag, KeyError):
            raise AppError("UPSTREAM_ERROR", "Secret could not be decrypted") from None
        wrap_nonce = os.urandom(12)
        wrapped = AESGCM(self._keys[self.current_version]).encrypt(wrap_nonce, dek, a)
        return struct.pack(">I", self.current_version) + wrap_nonce + wrapped + blob[64:]
