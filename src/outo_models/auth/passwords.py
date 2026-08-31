"""Password hashing wrappers for outo-models.

These functions are the only place the application should call argon2id for
human-typed passwords. `utils.hashing` deliberately keeps a separate (faster)
argon2 profile for non-interactive secrets such as token fingerprints — the
trade-offs differ: passwords need to be expensive to brute-force, tokens
need to verify in the hot path of an API request.

Constants here MUST stay aligned with `utils.hashing._HASHER` for tokens that
happen to be reused as passwords (none today, but the property must hold so
that a future migration stays mechanical).
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

# OWASP "Password Storage Cheat Sheet" argon2id minimums for *passwords*
# (slightly stronger than the fingerprint profile in `utils.hashing`,
# because passwords face offline brute-force after a DB leak).
_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1)


def hash_password(password: str) -> str:
    """Return an argon2id-encoded hash of `password`.

    A fresh random salt is generated on every call, so two hashes of the
    same password are always distinct.
    """
    return _PASSWORD_HASHER.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    """Return True iff `password` reproduces `hashed`.

    Verification failures (bad hash, wrong password, malformed blob) all
    degrade to `False` — never raise. This is the contract HTTP handlers
    rely on to avoid leaking which side of the comparison failed.
    """
    try:
        _PASSWORD_HASHER.verify(hashed, password)
    except (VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(hashed: str) -> bool:
    """Return True iff `hashed` was minted under weaker parameters than the current hasher.

    Callers should rehash and persist the new digest after a successful login
    when this returns True — a parameter-rotation upgrade pattern that costs
    nothing on the steady state and only one extra hash on rotation day.

    A malformed blob is treated as "needs rehash": the only safe thing to
    do with it is to overwrite it with a fresh hash on next successful login.
    """
    try:
        return _PASSWORD_HASHER.check_needs_rehash(hashed)
    except InvalidHashError:
        return True
