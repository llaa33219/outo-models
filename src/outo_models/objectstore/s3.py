"""S3-compatible object store backend for Git LFS.

`S3ObjectStore` implements the `ObjectStore` protocol against any
S3-compatible endpoint (AWS S3, MinIO, Cloudflare R2, ...). The signing
layer is hand-rolled on top of `hashlib` / `hmac` / `urllib.parse` so the
project stays free of `boto3` / `aioboto3` / `minio` SDKs.

Two signing entry points:

* `presign_url(...)` — query-string-presigned URL (for upload / download
  actions the LFS client uses directly; nothing in `LfsAction.headers`).
* `sign_request(...)` — header-signed request (for `HEAD` / `DELETE`
  calls the server issues server-side to inspect / remove objects).

Both share the same canonical-request + signing-key primitives, so any
bug in the SigV4 layer shows up in both paths.

Error mapping:
* 404 on HEAD → `has_object()` returns False / `object_size()` returns None.
* 204 / 404 on DELETE → `delete_object()` is a no-op (idempotent).
* Any other 4xx / 5xx, or a network error → `OutoError(code="s3_upstream")`.

Secrets hygiene: the secret key never appears in `repr()`, in any raised
exception, or in any log line. The store validates non-empty credentials
at construction time and refuses to operate otherwise.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

import httpx

from outo_models.exceptions import ConfigError, OutoError
from outo_models.objectstore.base import LfsAction
from outo_models.utils.time import utcnow

# ---------------------------------------------------------------------------
# Module-level constants. Everything in this block is referenced by both
# `presign_url()` and `sign_request()` — keep them as module-level so the
# algorithm is auditable in one place.
# ---------------------------------------------------------------------------

_SHA256 = "sha256"  # hashlib algorithm identifier for HMAC
_ALGORITHM: Final[str] = "AWS4-HMAC-SHA256"
_SERVICE: Final[str] = "s3"
_SIGNED_HEADERS_PRESIGN: Final[str] = "host"
_PAYLOAD_HASH_PRESIGN: Final[str] = "UNSIGNED-PAYLOAD"
_PAYLOAD_HASH_EMPTY: Final[str] = hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# SigV4 primitives. Each function has exactly one job and is unit-tested
# via `tests/unit/test_sigv4.py`.
# ---------------------------------------------------------------------------


def _uri_encode(value: str, *, encode_slash: bool = False) -> str:
    """RFC 3986 percent-encoding per AWS SigV4.

    Unreserved characters (`A-Z`, `a-z`, `0-9`, `-`, `.`, `_`, `~`) are
    left intact; everything else is encoded as `%XX`. The slash is
    preserved by default because SigV4 canonical URIs and canonical query
    strings both treat `/` as a structural separator — pass
    `encode_slash=True` only when encoding individual name=value tokens
    for the canonical query.
    """
    safe = "" if encode_slash else "/-._~"
    return quote(value, safe=safe)


def _encode_path(path: str) -> str:
    """URI-encode each `/`-separated path segment while preserving the slashes.

    SigV4 canonical URIs treat slashes between segments as path
    separators (not encoded), but characters inside each segment are
    percent-encoded per RFC 3986. Encoding the whole path as one blob
    would encode the structural slashes too — and the signature would
    no longer match the bytes the server receives.
    """
    return "/".join(_uri_encode(segment, encode_slash=True) for segment in path.split("/"))


def _signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    """Derive the SigV4 signing key (`kSigning`) for the given scope.

    The chain is fixed by the spec:

        kDate    = HMAC("AWS4" + secret, date_stamp)
        kRegion  = HMAC(kDate, region)
        kService = HMAC(kRegion, service)
        kSigning = HMAC(kService, "aws4_request")
    """
    k_date = hmac.new(
        f"AWS4{secret_key}".encode(),
        date_stamp.encode("utf-8"),
        _SHA256,
    ).digest()
    k_region = hmac.new(k_date, region.encode("utf-8"), _SHA256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), _SHA256).digest()
    return hmac.new(k_service, b"aws4_request", _SHA256).digest()


def _split_endpoint(endpoint: str) -> tuple[str, str]:
    """Split an S3 endpoint into (origin, host).

    `origin` is `scheme://host[:port]` — used as the URL prefix for the
    final presigned URL. `host` is the lowercase authority component the
    SigV4 `host` canonical header carries.

    Raises:
        ConfigError: When `endpoint` is missing a scheme or host.
    """
    parts = endpoint.split("://", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ConfigError(f"invalid s3_endpoint {endpoint!r}: expected scheme://host")
    scheme, rest = parts
    host = rest.split("/", 1)[0]
    if not host:
        raise ConfigError(f"invalid s3_endpoint {endpoint!r}: missing host")
    return f"{scheme}://{host}", host


def _canonical_query_string(params: dict[str, str]) -> str:
    """Build the SigV4 canonical query string.

    Parameters are percent-encoded per RFC 3986 (slash encoded), joined
    by `&`, and sorted lexicographically by name. Equal names are not
    possible here because SigV4 presign uses a single value per name.
    """
    return "&".join(
        f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
        for k, v in sorted(params.items())
    )


def _canonical_request(
    *,
    method: str,
    canonical_uri: str,
    canonical_query: str,
    canonical_headers: str,
    signed_headers: str,
    payload_hash: str,
) -> str:
    """Build the SigV4 canonical request string.

    Each component is separated by a literal newline; the final newline
    before `signed_headers` and `payload_hash` is implicit (the empty
    line at the end of the headers block).
    """
    return (
        f"{method}\n"
        f"{canonical_uri}\n"
        f"{canonical_query}\n"
        f"{canonical_headers}\n"
        f"{signed_headers}\n"
        f"{payload_hash}"
    )


def _string_to_sign(
    algorithm: str, amz_date: str, credential_scope: str, hashed_canonical: str
) -> str:
    """Build the SigV4 string-to-sign."""
    return f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashed_canonical}"


def _derive_signature(k_signing: bytes, string_to_sign: str) -> str:
    """Final SigV4 signature: hex(HMAC(kSigning, string_to_sign))."""
    return hmac.new(k_signing, string_to_sign.encode("utf-8"), _SHA256).hexdigest()


def _format_amz_dates(now: datetime) -> tuple[str, str]:
    """Return `(amz_date, date_stamp)` from a datetime.

    Naive datetimes are interpreted as UTC (defensive — callers should
    always pass timezone-aware). Aware datetimes in other zones are
    converted to UTC.
    """
    if now.tzinfo is None:
        normalized = now.replace(tzinfo=UTC)
    elif now.tzinfo != UTC:
        normalized = now.astimezone(UTC)
    else:
        normalized = now
    return (
        normalized.strftime("%Y%m%dT%H%M%SZ"),
        normalized.strftime("%Y%m%d"),
    )


# ---------------------------------------------------------------------------
# Public signing entry points. Exposed at module level (not methods on
# S3ObjectStore) so they can be unit-tested in isolation against the AWS
# test-vector reference.
# ---------------------------------------------------------------------------


def presign_url(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str,
    region: str,
    access_key: str,
    secret_key: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """Build a SigV4 query-string-presigned URL.

    Returns a path-style URL of the form:

        {endpoint}/{bucket}/{key}?X-Amz-Algorithm=...&X-Amz-Credential=...&...

    All required X-Amz-* query parameters are included; the final
    X-Amz-Signature is the last query parameter. The signed scope covers
    the host header only — payloads are not signed because the
    LFS client PUTs opaque bytes against the URL.

    Args:
        method: HTTP verb in uppercase (`"GET"` / `"PUT"`). SigV4
            canonicalizes the method verbatim, so callers must normalize.
        endpoint: S3-compatible endpoint URL without trailing slash.
        bucket: S3 bucket name.
        key: Object key (without any leading slash).
        region: AWS region / S3 region (e.g. `"us-east-1"`).
        access_key: S3 access key id.
        secret_key: S3 secret access key. Never logged; never embedded
            in the returned URL.
        ttl_seconds: URL validity in seconds; encoded as `X-Amz-Expires`.
        now: Wall-clock time used for `X-Amz-Date`. Defaults to
            `outo_models.utils.time.utcnow()`; injected for tests so the
            signature is reproducible.

    Returns:
        The fully-presigned URL.
    """
    if not method:
        raise ValueError("presign_url: method must be non-empty")
    if ttl_seconds <= 0:
        raise ValueError(f"presign_url: ttl_seconds must be positive, got {ttl_seconds}")

    if now is None:
        now = utcnow()
    amz_date, date_stamp = _format_amz_dates(now)
    origin, host = _split_endpoint(endpoint)
    credential_scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"

    encoded_bucket = _encode_path(bucket)
    encoded_key = _encode_path(key)
    canonical_uri = f"/{encoded_bucket}/{encoded_key}"

    params = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(ttl_seconds),
        "X-Amz-SignedHeaders": _SIGNED_HEADERS_PRESIGN,
    }
    canonical_query = _canonical_query_string(params)
    canonical_headers = f"host:{host}\n"

    canonical = _canonical_request(
        method=method,
        canonical_uri=canonical_uri,
        canonical_query=canonical_query,
        canonical_headers=canonical_headers,
        signed_headers=_SIGNED_HEADERS_PRESIGN,
        payload_hash=_PAYLOAD_HASH_PRESIGN,
    )
    hashed_canonical = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    string_to_sign = _string_to_sign(_ALGORITHM, amz_date, credential_scope, hashed_canonical)
    k_signing = _signing_key(secret_key, date_stamp, region, _SERVICE)
    signature = _derive_signature(k_signing, string_to_sign)

    # The signature is appended after the canonical (sorted) query; AWS
    # never re-sorts the URL after signing, so `X-Amz-Signature` always
    # lands at the end.
    query = f"{canonical_query}&X-Amz-Signature={signature}"
    return f"{origin}/{encoded_bucket}/{encoded_key}?{query}"


def sign_request(
    *,
    method: str,
    endpoint: str,
    bucket: str,
    key: str,
    region: str,
    access_key: str,
    secret_key: str,
    payload: bytes = b"",
    extra_signed_headers: dict[str, str] | None = None,
    now: datetime | None = None,
) -> tuple[str, dict[str, str]]:
    """Sign a request for direct (header-based) SigV4 — for `HEAD` / `DELETE`.

    Returns `(canonical_uri, headers)` where:
    * `canonical_uri` is the path-style path the caller feeds to
      `httpx.AsyncClient` (`/{bucket}/{key}`).
    * `headers` includes the `Authorization`, `x-amz-date`, and
      `x-amz-content-sha256` headers. `host` is signed but not emitted
      separately because `httpx` derives it from the URL.

    Args:
        method: HTTP verb in uppercase.
        endpoint: S3-compatible endpoint URL.
        bucket: S3 bucket name.
        key: Object key.
        region: AWS / S3 region.
        access_key: S3 access key id.
        secret_key: S3 secret access key. Never embedded in headers.
        payload: Request body bytes; defaults to empty.
        extra_signed_headers: Additional signed headers beyond `host`.
            Values are stripped of leading / trailing whitespace.
        now: Wall-clock injection; defaults to `utcnow()`.

    Returns:
        `(canonical_uri, headers)` ready to feed into an httpx request.
    """
    if not method:
        raise ValueError("sign_request: method must be non-empty")

    if now is None:
        now = utcnow()
    amz_date, date_stamp = _format_amz_dates(now)
    _, host = _split_endpoint(endpoint)
    credential_scope = f"{date_stamp}/{region}/{_SERVICE}/aws4_request"

    canonical_uri = f"/{_encode_path(bucket)}/{_encode_path(key)}"

    # Build canonical headers; `host` is always present, additional headers
    # are sorted by name. Lower-cased keys per SigV4 spec.
    signed: dict[str, str] = {"host": host}
    if extra_signed_headers:
        for name, value in extra_signed_headers.items():
            signed[name.lower()] = value.strip()
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in sorted(signed.items()))
    signed_headers = ";".join(sorted(signed.keys()))
    payload_hash = hashlib.sha256(payload).hexdigest()

    canonical = _canonical_request(
        method=method,
        canonical_uri=canonical_uri,
        canonical_query="",
        canonical_headers=canonical_headers,
        signed_headers=signed_headers,
        payload_hash=payload_hash,
    )
    hashed_canonical = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    string_to_sign = _string_to_sign(_ALGORITHM, amz_date, credential_scope, hashed_canonical)
    k_signing = _signing_key(secret_key, date_stamp, region, _SERVICE)
    signature = _derive_signature(k_signing, string_to_sign)

    authorization = (
        f"{_ALGORITHM} "
        f"Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    headers: dict[str, str] = {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    return canonical_uri, headers


# ---------------------------------------------------------------------------
# S3ObjectStore — the LFS-side implementation of `ObjectStore`.
# ---------------------------------------------------------------------------


def _shard_object_key(prefix: str, oid: str) -> str:
    """Build the on-disk / on-bucket key for `oid`.

    Layout: `<prefix>/<aa>/<bb>/<oid>` — two-level sharding keeps any
    single directory small enough that list operations stay fast.
    """
    lower = oid.lower()
    return f"{prefix}/{lower[:2]}/{lower[2:4]}/{lower}"


def _validate_oid(oid: str) -> None:
    """Reject anything but a 64-char lowercase-hex sha256 oid.

    The LFS spec fixes oid to sha256 hex; rejecting here keeps a path
    traversal attempt from ever reaching the SigV4 / S3 layer.
    """
    if not isinstance(oid, str) or len(oid) != 64:
        raise ValueError(f"oid must be 64 chars, got {len(oid) if isinstance(oid, str) else '?'}")
    for ch in oid.lower():
        if ch not in "0123456789abcdef":
            raise ValueError(f"oid contains non-hex characters: {oid!r}")


class S3ObjectStore:
    """S3-compatible LFS object store.

    Args:
        endpoint: S3-compatible endpoint URL.
        bucket: Bucket name.
        region: Region / region name.
        access_key: Access key id.
        secret_key: Secret access key. Never logged; never embedded in
            `repr()` or in any exception message.
        prefix: Object-key prefix (e.g. `"lfs"`).
        presign_ttl: TTL in seconds for upload / download actions.
        client: Optional pre-built `httpx.AsyncClient`. When supplied
            the store does NOT close it in `aclose()` — the caller
            retains ownership.

    Raises:
        ConfigError: At construction time when any of `endpoint`,
            `bucket`, `region`, `access_key`, `secret_key` is empty,
            when `presign_ttl` is non-positive, or when `endpoint` is
            not a parseable `scheme://host` URL.
    """

    name: Final[str] = "s3"

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        prefix: str,
        presign_ttl: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # ---- validation (refuses to construct on bad config) ----
        if not endpoint:
            raise ConfigError("S3 backend: s3_endpoint is empty")
        if not bucket:
            raise ConfigError("S3 backend: s3_bucket is empty")
        if not region:
            raise ConfigError("S3 backend: s3_region is empty")
        if not access_key:
            raise ConfigError("S3 backend: s3_access_key is empty")
        if not secret_key:
            raise ConfigError("S3 backend: s3_secret_key is empty")
        if presign_ttl <= 0:
            raise ConfigError(
                f"S3 backend: s3_presign_ttl_seconds must be positive (got {presign_ttl})"
            )
        # Validate endpoint parses; raises ConfigError on malformed.
        _split_endpoint(endpoint)

        self._endpoint = endpoint
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._prefix = prefix
        self._presign_ttl = presign_ttl
        self._client = client
        self._owns_client = client is None

    # ----- representation -----

    def __repr__(self) -> str:
        """`repr()` deliberately omits the secret key.

        Tests assert that the secret never appears in `repr()` — the
        standard redaction pattern. Credentials are referenced by
        attribute name only when needed.
        """
        return (
            f"S3ObjectStore(endpoint={self._endpoint!r}, bucket={self._bucket!r}, "
            f"region={self._region!r}, prefix={self._prefix!r})"
        )

    # ----- lifecycle -----

    async def __aenter__(self) -> S3ObjectStore:
        """Acquire a client on entry if one wasn't injected.

        When a client was injected via the constructor we leave it alone
        and the store does not own it — the caller is responsible for
        closing it. When no client was injected we lazily create one
        on first use, and own it for the lifetime of the store.
        """
        self._require_client()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the owned client (no-op when one was injected)."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        """Return the active client, lazily creating one when needed."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
            self._owns_client = True
        return self._client

    # ----- ObjectStore API -----

    async def make_upload_action(
        self,
        *,
        owner: str,
        repo: str,
        oid: str,
        size: int,
    ) -> LfsAction:
        """Return a presigned PUT URL the LFS client uploads to.

        `owner` / `repo` / `size` are part of the protocol but unused
        for path-style S3: the bucket is fixed by config, and the
        size goes into the upload stream itself, not the URL.
        """
        del owner, repo, size
        _validate_oid(oid)
        key = _shard_object_key(self._prefix, oid)
        href = presign_url(
            method="PUT",
            endpoint=self._endpoint,
            bucket=self._bucket,
            key=key,
            region=self._region,
            access_key=self._access_key,
            secret_key=self._secret_key,
            ttl_seconds=self._presign_ttl,
        )
        return LfsAction(href=href, headers={}, expires_in=self._presign_ttl)

    async def make_download_action(
        self,
        *,
        owner: str,
        repo: str,
        oid: str,
        size: int,
    ) -> LfsAction:
        """Return a presigned GET URL the LFS client downloads from."""
        del owner, repo, size
        _validate_oid(oid)
        key = _shard_object_key(self._prefix, oid)
        href = presign_url(
            method="GET",
            endpoint=self._endpoint,
            bucket=self._bucket,
            key=key,
            region=self._region,
            access_key=self._access_key,
            secret_key=self._secret_key,
            ttl_seconds=self._presign_ttl,
        )
        return LfsAction(href=href, headers={}, expires_in=self._presign_ttl)

    async def has_object(self, oid: str) -> bool:
        """HEAD the object and translate the response onto a bool.

        200 → True, 404 → False, anything else → `OutoError("s3_upstream")`.
        """
        _validate_oid(oid)
        key = _shard_object_key(self._prefix, oid)
        uri, headers = sign_request(
            method="HEAD",
            endpoint=self._endpoint,
            bucket=self._bucket,
            key=key,
            region=self._region,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )
        client = self._require_client()
        try:
            response = await client.head(uri, headers=headers)
        except httpx.HTTPError as exc:
            raise OutoError(
                f"S3 HEAD network error for {oid[:8]}: {exc}",
                code="s3_upstream",
            ) from exc
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise OutoError(
            f"S3 HEAD {oid[:8]} returned {response.status_code}",
            code="s3_upstream",
        )

    async def object_size(self, oid: str) -> int | None:
        """HEAD the object and return `Content-Length` if present.

        404 → None, any other non-200 → `OutoError("s3_upstream")`. When
        the response is 200 but the header is missing we return None
        rather than guess — callers treat None as "size unknown".
        """
        _validate_oid(oid)
        key = _shard_object_key(self._prefix, oid)
        uri, headers = sign_request(
            method="HEAD",
            endpoint=self._endpoint,
            bucket=self._bucket,
            key=key,
            region=self._region,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )
        client = self._require_client()
        try:
            response = await client.head(uri, headers=headers)
        except httpx.HTTPError as exc:
            raise OutoError(
                f"S3 HEAD network error for {oid[:8]}: {exc}",
                code="s3_upstream",
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise OutoError(
                f"S3 HEAD {oid[:8]} returned {response.status_code}",
                code="s3_upstream",
            )
        length = response.headers.get("Content-Length")
        if length is None:
            return None
        try:
            return int(length)
        except ValueError as exc:
            raise OutoError(
                f"S3 HEAD {oid[:8]} returned non-numeric Content-Length: {length!r}",
                code="s3_upstream",
            ) from exc

    async def delete_object(self, oid: str) -> None:
        """DELETE the object; idempotent on 204 and 404."""
        _validate_oid(oid)
        key = _shard_object_key(self._prefix, oid)
        uri, headers = sign_request(
            method="DELETE",
            endpoint=self._endpoint,
            bucket=self._bucket,
            key=key,
            region=self._region,
            access_key=self._access_key,
            secret_key=self._secret_key,
        )
        client = self._require_client()
        try:
            response = await client.delete(uri, headers=headers)
        except httpx.HTTPError as exc:
            raise OutoError(
                f"S3 DELETE network error for {oid[:8]}: {exc}",
                code="s3_upstream",
            ) from exc
        if response.status_code in (204, 404):
            return
        raise OutoError(
            f"S3 DELETE {oid[:8]} returned {response.status_code}",
            code="s3_upstream",
        )


__all__ = [
    "S3ObjectStore",
    "presign_url",
    "sign_request",
]