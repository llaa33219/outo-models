"""argon2id hashing for token fingerprints and similar opaque secrets.

Password hashing is intentionally NOT implemented here — the auth team owns
password wrappers (different parameters, peppering, breach checks, etc.).
This module is the shared primitive for "hash a non-password secret that we
must verify later" use cases.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# OWASP "Cheat Sheet — Password Storage" minimums for argon2id (2024).
#   time_cost = 3 iterations
#   memory_cost = 64 MiB (= 65536 KiB, the unit argon2-cffi uses)
#   parallelism = 1 (single thread per call)
_HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1)


def hash_secret(secret: str) -> str:
    """Return an argon2id-encoded hash of `secret`. Each call uses a fresh salt."""
    return _HASHER.hash(secret)


def verify_secret(hashed: str, secret: str) -> bool:
    """Return True iff `secret` reproduces `hashed`. Never raises on bad input."""
    try:
        _HASHER.verify(hashed, secret)
    except (VerificationError, InvalidHashError):
        return False
    return True
