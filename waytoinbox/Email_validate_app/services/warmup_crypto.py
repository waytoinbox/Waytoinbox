"""
Encryption for Warmup receiver OAuth refresh tokens.

Refresh tokens are longer-lived and more sensitive than an SMTP app password
a user can freely rotate (see so_smtp.py::decrypt_password, which uses
django.core.signing — tamper-evident but NOT confidential, readable by
anyone with DB access), so these get real encryption instead: Fernet
(symmetric, authenticated) via the already-pinned `cryptography` package,
with its own dedicated key — not derived from SECRET_KEY, so rotating one
doesn't silently affect the other.

WARMUP_TOKEN_ENCRYPTION_KEY is read lazily from the environment (not at
Django startup, unlike SECRET_KEY) so the rest of the app keeps working for
installs that haven't configured Warmup yet — only these functions raise,
and only when actually called.
"""

import os

from cryptography.fernet import Fernet, InvalidToken


def _get_fernet() -> Fernet:
    key = os.environ.get('WARMUP_TOKEN_ENCRYPTION_KEY', '').strip()
    if not key:
        raise EnvironmentError(
            "WARMUP_TOKEN_ENCRYPTION_KEY is not set in .env. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_token(plain: str) -> str:
    """Encrypt an OAuth refresh token for storage. Never log the input or output."""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a stored refresh token. Raises ValueError if the ciphertext is
    invalid/tampered (e.g. the encryption key was rotated) rather than a raw
    cryptography exception, so callers get one exception type to catch."""
    try:
        return _get_fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken:
        raise ValueError('Stored refresh token could not be decrypted — it may have been encrypted with a different key.')
