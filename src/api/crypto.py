"""Fernet-based encryption for sensitive credential storage.

The key is a random 32-byte Fernet key stored in a 0600 file beside the DB.
Old deployments that derived the key from a hardcoded passphrase keep their
existing key file (backward compatible); the passphrase path is gone.
"""

import os
from cryptography.fernet import Fernet

KEY_DIR = "/opt/barenoc/volumes/db"
KEY_FILE = os.path.join(KEY_DIR, "fernet.key")


def _get_key() -> bytes:
    """Get or create a random Fernet encryption key (file perms 0600)."""
    os.makedirs(KEY_DIR, exist_ok=True)
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            key = f.read().strip()
        if key:
            return key

    # Generate a fresh random key (32 bytes, urlsafe base64)
    key = Fernet.generate_key()
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    return key


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns base64-encoded ciphertext."""
    if not plaintext:
        return ""
    key = _get_key()
    f = Fernet(key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a base64-encoded ciphertext. Returns plaintext."""
    if not ciphertext:
        return ""
    try:
        key = _get_key()
        f = Fernet(key)
        return f.decrypt(ciphertext.encode()).decode()
    except Exception:
        return "[encrypted]"
