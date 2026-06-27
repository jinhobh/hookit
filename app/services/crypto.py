"""Encryption helpers for secrets stored at rest.

Webhook endpoint signing secrets are encrypted with Fernet (AES-128-CBC +
HMAC-SHA256) before being written to the database.  The key is read from
``settings.endpoint_secret_key``.  Plaintext secrets are never logged.
"""

from __future__ import annotations

import secrets

from cryptography.fernet import Fernet

from app.core.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().endpoint_secret_key.encode())


def generate_endpoint_secret() -> str:
    """Return a 32-byte (256-bit) hex secret suitable for HMAC signing."""
    return secrets.token_hex(32)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* and return a URL-safe base64 ciphertext string."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a ciphertext produced by :func:`encrypt_secret`."""
    return _fernet().decrypt(ciphertext.encode()).decode()
