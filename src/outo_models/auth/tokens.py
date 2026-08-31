"""Personal Access Token (PAT) engine for outo-models.

PATs are PASETO v4 *local* tokens: encrypted (not merely signed), authenticated,
and bound to a 32-byte symmetric key derived from the server's `secret_key`.
The plaintext is never persisted — only an argon2id fingerprint of the token
is stored, so a DB leak cannot be replayed against the API.

The payload schema is intentionally tiny:

    {"sub": <subject>, "scopes": [<scope>, ...], "exp": <unix-seconds>}

`sub` identifies the principal the PAT acts on behalf of (a user or a service
account); `scopes` are matched against `Scope` values; `exp` is a Unix
timestamp integer — NOT the ISO-8601 string PASETO's registered-claim parser
expects, which keeps `TokenClaims.expires_at` a clean timezone-aware datetime.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pyseto
from pyseto.exceptions import DecryptError

from outo_models.exceptions import UnauthorizedError
from outo_models.utils.hashing import hash_secret, verify_secret

#: Default lifetime of an issued PAT: 90 days.
DEFAULT_TOKEN_TTL_SECONDS = 7_776_000  # 90 * 24 * 3600


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Verified payload of a Personal Access Token.

    `expires_at` is timezone-aware UTC. `scopes` preserves the order in which
    they were issued (callers should not assume ordering carries meaning).
    """

    subject: str
    scopes: list[str] = field(default_factory=list)
    expires_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class TokenService:
    """Mint and verify PASETO v4 local tokens."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"PASETO v4 local key must be exactly 32 bytes, got {len(key)}")
        self._key = pyseto.Key.new(version=4, purpose="local", key=key)

    @classmethod
    def from_secret(cls, secret: str) -> TokenService:
        """Derive a 32-byte PASETO key from an arbitrary server secret via SHA-256.

        `secret` MUST be non-empty — deriving a key from an empty secret
        would silently produce the well-known SHA-256 of the empty string
        and sign every install with the same key. Refusing the empty string
        forces the bug to surface at boot, not at first breach.
        """
        if not secret:
            raise ValueError("secret must be a non-empty string")
        return cls(hashlib.sha256(secret.encode("utf-8")).digest())

    def issue(
        self,
        subject: str,
        scopes: list[str],
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> str:
        """Return a fresh, signed token carrying `subject`, `scopes`, and an `exp` claim."""
        expires_at = int(datetime.now(tz=UTC).timestamp()) + ttl_seconds
        payload = {"sub": subject, "scopes": list(scopes), "exp": expires_at}
        token_bytes = pyseto.encode(self._key, payload=payload, serializer=json)
        return token_bytes.decode("ascii")

    def verify(self, token: str) -> TokenClaims:
        """Return a `TokenClaims` iff the token is signed by us and unexpired.

        Raises:
            UnauthorizedError: when the token is missing, malformed, signed by
                a different key, contains a non-dict body, or has expired.
        """
        if not token:
            raise UnauthorizedError("Empty token")
        try:
            decoded = pyseto.decode(self._key, token)
            raw = decoded.payload
            if not isinstance(raw, (bytes, bytearray)):
                # pyseto's stubs type payload as `bytes | dict[str, Any]`;
                # the JSON serializer always emits bytes here, but the
                # annotation allows a deserializer-driven dict passthrough.
                raise UnauthorizedError("Unexpected token payload type")
            payload = json.loads(bytes(raw).decode("utf-8"))
        except (DecryptError, ValueError, json.JSONDecodeError) as exc:
            raise UnauthorizedError("Invalid or tampered token") from exc
        if not isinstance(payload, dict):
            raise UnauthorizedError("Invalid token payload")
        try:
            subject = payload["sub"]
            scopes = payload["scopes"]
            exp = payload["exp"]
        except KeyError as exc:
            raise UnauthorizedError("Malformed token claims") from exc
        if not isinstance(subject, str):
            raise UnauthorizedError("Invalid token subject")
        if not isinstance(scopes, list) or any(not isinstance(s, str) for s in scopes):
            raise UnauthorizedError("Invalid token scopes")
        if not isinstance(exp, int):
            raise UnauthorizedError("Invalid token expiration")
        expires_at = datetime.fromtimestamp(exp, tz=UTC)
        now = datetime.now(tz=UTC)
        if expires_at <= now:
            raise UnauthorizedError("Token expired")
        return TokenClaims(subject=subject, scopes=scopes, expires_at=expires_at)


def fingerprint(token: str) -> str:
    """Return an argon2id-encoded fingerprint suitable for DB storage.

    The fingerprint is what lets us recognize a token on subsequent
    authentication without ever persisting the plaintext.
    """
    return hash_secret(token)


def match_fingerprint(hashed: str, token: str) -> bool:
    """Return True iff `token` reproduces `hashed`. Never raises on bad input."""
    return verify_secret(hashed, token)
