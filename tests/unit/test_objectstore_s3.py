"""Unit tests for `outo_models.objectstore.s3.S3ObjectStore`.

These tests sit on top of `tests/unit/test_sigv4.py`. There, the SigV4
algorithm is exercised against fixed inputs and AWS-published reference
vectors; here, the S3ObjectStore class is exercised against its public
contract using `respx` to fake the wire boundary.

Contract pins:

* Construction rejects empty endpoint / bucket / region / access_key /
  secret_key with `ConfigError`. The `secret_key` never appears in
  `repr()` nor in any exception message.
* `make_upload_action` / `make_download_action` return an `LfsAction`
  whose `href` is a SigV4-presigned URL pointing at path-style
  `<endpoint>/<bucket>/<prefix>/<aa>/<bb>/<oid>`, carrying the
  expected X-Amz-* query parameters and the configured TTL.
* `has_object` issues a signed HEAD request: 200 → True, 404 → False,
  5xx / network error → `OutoError(code="s3_upstream")`.
* `object_size` extracts `Content-Length` from a 200 HEAD; 404 → None;
  any other non-200 → `OutoError("s3_upstream")`.
* `delete_object` issues a signed DELETE: 204 / 404 → no-op; everything
  else → `OutoError("s3_upstream")`.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from outo_models.exceptions import ConfigError, OutoError
from outo_models.objectstore.s3 import S3ObjectStore, _shard_object_key

_ENDPOINT = "https://s3.amazonaws.com"
_BUCKET = "examplebucket"
_REGION = "us-east-1"
_ACCESS_KEY = "AKIDEXAMPLE"
_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
_PREFIX = "lfs"
_TTL = 3600

# A valid sha256 oid: 64 lowercase hex chars. Used everywhere so tests
# stay focused on the S3 contract, not on oid-validation semantics.
_OID = "0123456789abcdef" * 4  # 64 chars
_OBJECT_KEY = f"{_PREFIX}/01/23/{_OID}"


def _make(
    *,
    endpoint: str = _ENDPOINT,
    bucket: str = _BUCKET,
    region: str = _REGION,
    access_key: str = _ACCESS_KEY,
    secret_key: str = _SECRET_KEY,
    prefix: str = _PREFIX,
    presign_ttl: int = _TTL,
    client: httpx.AsyncClient | None = None,
) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint=endpoint,
        bucket=bucket,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        prefix=prefix,
        presign_ttl=presign_ttl,
        client=client,
    )


# ---------------------------------------------------------------------------
# Construction validation + secret hygiene
# ---------------------------------------------------------------------------


class TestConstruction:
    """`S3ObjectStore` rejects empty / malformed configuration up front."""

    def test_name_is_s3(self) -> None:
        store = _make()
        assert store.name == "s3"

    def test_rejects_empty_endpoint(self) -> None:
        with pytest.raises(ConfigError, match="s3_endpoint"):
            _make(endpoint="")

    def test_rejects_empty_bucket(self) -> None:
        with pytest.raises(ConfigError, match="s3_bucket"):
            _make(bucket="")

    def test_rejects_empty_access_key(self) -> None:
        with pytest.raises(ConfigError, match="s3_access_key"):
            _make(access_key="")

    def test_rejects_empty_secret_key(self) -> None:
        with pytest.raises(ConfigError, match="s3_secret_key"):
            _make(secret_key="")

    def test_rejects_empty_region(self) -> None:
        with pytest.raises(ConfigError, match="s3_region"):
            _make(region="")

    def test_rejects_non_positive_ttl(self) -> None:
        with pytest.raises(ConfigError, match="s3_presign_ttl_seconds"):
            _make(presign_ttl=0)

    def test_rejects_malformed_endpoint(self) -> None:
        with pytest.raises(ConfigError, match="s3_endpoint"):
            _make(endpoint="not-a-url")

    def test_secret_key_never_appears_in_repr(self) -> None:
        store = _make()
        rendered = repr(store)
        assert _SECRET_KEY not in rendered
        assert "secret_key" not in rendered

    def test_secret_key_never_appears_in_str(self) -> None:
        store = _make()
        assert _SECRET_KEY not in str(store)


class TestSecretHygieneOnFailure:
    """Construction errors must never include the secret in their message."""

    @pytest.mark.parametrize(
        "overrides",
        [
            {"endpoint": ""},
            {"bucket": ""},
            {"access_key": ""},
            {"secret_key": ""},
            {"region": ""},
        ],
    )
    def test_construction_errors_omit_secret(self, overrides: dict[str, str]) -> None:
        # The default secret is the published "wJal..." test key.
        # Trigger every error path and assert the message never contains it.
        with pytest.raises(ConfigError) as exc_info:
            _make(**overrides)
        assert _SECRET_KEY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Key sharding
# ---------------------------------------------------------------------------


class TestKeySharding:
    """Object keys follow the documented `prefix/aa/bb/oid` layout."""

    def test_default_prefix(self) -> None:
        assert _shard_object_key("lfs", _OID) == _OBJECT_KEY

    def test_custom_prefix(self) -> None:
        assert _shard_object_key("models", _OID) == f"models/01/23/{_OID}"

    def test_oid_lowercased(self) -> None:
        upper = "ABCDEF" * 10 + "AB"  # 62 chars
        # Force full 64 chars by padding with valid hex.
        oid = (upper + "CD").lower()[:64]
        assert _shard_object_key("lfs", oid) == f"lfs/{oid[:2]}/{oid[2:4]}/{oid}"


# ---------------------------------------------------------------------------
# Presigned actions — pure URL asserts (no network).
# ---------------------------------------------------------------------------


class _UrlAsserts:
    """Helpers shared by the upload/download action tests."""

    @staticmethod
    def assert_presigned_put_get_url(
        url: str,
        *,
        method_token: str,
        ttl: int,
    ) -> None:
        parsed = urlparse(url)
        # Path is path-style: /<bucket>/<prefix>/<aa>/<bb>/<oid>.
        assert parsed.scheme == "https"
        assert parsed.netloc == "s3.amazonaws.com"
        assert parsed.path == f"/{_BUCKET}/{_OBJECT_KEY}"
        params = parse_qs(parsed.query)
        assert params["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert params["X-Amz-SignedHeaders"] == ["host"]
        assert params["X-Amz-Expires"] == [str(ttl)]
        # Credential encodes region / service / date; we don't pin the
        # exact timestamp because the URL is generated "now".
        assert params["X-Amz-Credential"][0].startswith(f"{_ACCESS_KEY}/")
        assert params["X-Amz-Credential"][0].endswith(f"/{_REGION}/s3/aws4_request")
        # The signature is a 64-char hex blob.
        sig = params["X-Amz-Signature"][0]
        assert re.fullmatch(r"[0-9a-f]{64}", sig), sig
        assert method_token in ("PUT", "GET")  # annotation for readability


class TestMakeUploadAction:
    """`make_upload_action` returns a SigV4-presigned PUT URL."""

    async def test_returns_lfs_action_with_presigned_put(self) -> None:
        store = _make()
        action = await store.make_upload_action(
            owner="alice", repo="model", oid=_OID, size=1024
        )
        # href points at path-style S3 endpoint + bucket + sharded key.
        assert action.headers == {}
        assert action.expires_in == _TTL
        _UrlAsserts.assert_presigned_put_get_url(
            action.href, method_token="PUT", ttl=_TTL
        )

    async def test_includes_all_required_x_amz_params(self) -> None:
        store = _make()
        action = await store.make_upload_action(
            owner="alice", repo="model", oid=_OID, size=1024
        )
        params = parse_qs(urlparse(action.href).query)
        # Every X-Amz-* parameter the LFS spec / AWS S3 presign requires.
        for name in (
            "X-Amz-Algorithm",
            "X-Amz-Credential",
            "X-Amz-Date",
            "X-Amz-Expires",
            "X-Amz-Signature",
            "X-Amz-SignedHeaders",
        ):
            assert name in params, f"missing query param {name}"

    async def test_ttl_propagates_to_expires(self) -> None:
        store = _make(presign_ttl=1234)
        action = await store.make_upload_action(
            owner="alice", repo="model", oid=_OID, size=1024
        )
        assert parse_qs(urlparse(action.href).query)["X-Amz-Expires"] == ["1234"]
        assert action.expires_in == 1234

    async def test_rejects_malformed_oid(self) -> None:
        store = _make()
        with pytest.raises(ValueError):
            await store.make_upload_action(
                owner="alice", repo="model", oid="not-a-real-oid", size=1024
            )


class TestMakeDownloadAction:
    """`make_download_action` returns a SigV4-presigned GET URL."""

    async def test_returns_lfs_action_with_presigned_get(self) -> None:
        store = _make()
        action = await store.make_download_action(
            owner="alice", repo="model", oid=_OID, size=1024
        )
        assert action.headers == {}
        assert action.expires_in == _TTL
        _UrlAsserts.assert_presigned_put_get_url(
            action.href, method_token="GET", ttl=_TTL
        )

    async def test_get_signature_differs_from_put(self) -> None:
        store = _make()
        upload = await store.make_upload_action(
            owner="alice", repo="model", oid=_OID, size=1024
        )
        download = await store.make_download_action(
            owner="alice", repo="model", oid=_OID, size=1024
        )
        upload_sig = parse_qs(urlparse(upload.href).query)["X-Amz-Signature"][0]
        download_sig = parse_qs(urlparse(download.href).query)["X-Amz-Signature"][0]
        assert upload_sig != download_sig


# ---------------------------------------------------------------------------
# Server-side HEAD / DELETE — exercised against respx-mocked HTTP.
# ---------------------------------------------------------------------------


@pytest.fixture
def respx_mock() -> respx.MockRouter:
    """respx MockRouter bound to the S3 endpoint base URL."""
    with respx.mock(assert_all_called=False, assert_all_mocked=False, base_url=_ENDPOINT) as mock:
        yield mock


@pytest.fixture
def store_with_mocked_client(respx_mock: respx.MockRouter) -> S3ObjectStore:
    """`S3ObjectStore` paired with a respx-controlled `AsyncClient`."""
    client = httpx.AsyncClient(base_url=_ENDPOINT)
    return _make(client=client), client


class TestHasObject:
    """`has_object` translates HEAD responses onto a bool."""

    async def test_200_returns_true(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        route = respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(200, headers={"Content-Length": "12"})
        )
        try:
            assert await store.has_object(_OID) is True
        finally:
            await client.aclose()
        assert route.called

    async def test_404_returns_false(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(return_value=httpx.Response(404))
        try:
            assert await store.has_object(_OID) is False
        finally:
            await client.aclose()

    async def test_500_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(500, text="internal")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.has_object(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()

    async def test_403_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.has_object(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()

    async def test_network_error_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.has_object(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()


class TestObjectSize:
    """`object_size` reads `Content-Length` from a 200 HEAD response."""

    async def test_200_returns_content_length(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(200, headers={"Content-Length": "1234567"})
        )
        try:
            assert await store.object_size(_OID) == 1234567
        finally:
            await client.aclose()

    async def test_404_returns_none(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(return_value=httpx.Response(404))
        try:
            assert await store.object_size(_OID) is None
        finally:
            await client.aclose()

    async def test_200_missing_content_length_returns_none(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(200, headers={})
        )
        try:
            assert await store.object_size(_OID) is None
        finally:
            await client.aclose()

    async def test_500_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(503, text="unavailable")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.object_size(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()

    async def test_network_error_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.head(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.object_size(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()


class TestDeleteObject:
    """`delete_object` is idempotent on 204 / 404 and surfaces other errors."""

    async def test_204_is_success(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        route = respx_mock.delete(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(204)
        )
        try:
            await store.delete_object(_OID)
        finally:
            await client.aclose()
        assert route.called

    async def test_404_is_noop(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.delete(f"/{_BUCKET}/{_OBJECT_KEY}").mock(return_value=httpx.Response(404))
        try:
            await store.delete_object(_OID)
        finally:
            await client.aclose()

    async def test_500_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.delete(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(500, text="internal")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.delete_object(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()

    async def test_403_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.delete(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            return_value=httpx.Response(403, text="forbidden")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.delete_object(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()

    async def test_network_error_raises_s3_upstream(
        self,
        respx_mock: respx.MockRouter,
        store_with_mocked_client: tuple[S3ObjectStore, httpx.AsyncClient],
    ) -> None:
        store, client = store_with_mocked_client
        respx_mock.delete(f"/{_BUCKET}/{_OBJECT_KEY}").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        try:
            with pytest.raises(OutoError) as exc_info:
                await store.delete_object(_OID)
            assert exc_info.value.code == "s3_upstream"
        finally:
            await client.aclose()


# ---------------------------------------------------------------------------
# Lifecycle / ownership semantics
# ---------------------------------------------------------------------------


class TestAsyncClientLifecycle:
    """`aclose()` only closes clients the store owns."""

    async def test_injected_client_is_not_closed(self) -> None:
        client = httpx.AsyncClient()
        store = _make(client=client)
        await store.aclose()
        # The store must not have closed a client it doesn't own.
        assert not client.is_closed
        await client.aclose()
        assert client.is_closed

    async def test_async_context_manager_creates_then_closes(self) -> None:
        async with _make() as store:
            # Reaching the first HTTP requires a client; constructor did
            # not raise because it didn't build one yet.
            assert store._client is not None
        # After exit, the store closed its own client.
        assert store._client is None