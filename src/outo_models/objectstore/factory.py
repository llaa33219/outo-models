"""Build the active LFS object store from `Settings`.

`create_object_store` is the single entry point the rest of the code uses
to materialize an `ObjectStore`. The factory is kept separate from the
backend implementations so WP-20 can land `S3ObjectStore` without
modifying any handler code.

S3 is lazy-imported: the dev / CI environments we test in do NOT have
boto3 installed, so the import only fires when an operator actually
selects `"s3"`. A `ConfigError` with a Korean message surfaces the
missing dependency / missing settings so the operator can act on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from outo_models.config import Settings
from outo_models.exceptions import ConfigError
from outo_models.objectstore.base import ObjectStore

if TYPE_CHECKING:
    pass


def create_object_store(
    settings: Settings,
    *,
    base_url_override: str | None = None,
) -> ObjectStore:
    """Construct the configured backend.

    `base_url_override` lets the ASGI dispatcher hand the store the
    actual request origin (scheme + host + port) so test rigs bound to
    a non-default port still hand clients a URL that resolves. Production
    code leaves this `None` and the store uses `Settings.base_url`.

    Returns:
        An `ObjectStore` ready to serve upload / download actions for
        git-lfs. The local backend is the production default; the S3
        backend requires both an installed `outo_models.objectstore.s3`
        module (added by WP-20) and a fully-populated `Settings` —
        missing fields raise a `ConfigError` before the backend tries
        to sign anything.
    """
    backend = settings.lfs_backend
    effective_base = base_url_override or settings.base_url
    if backend == "local":
        from outo_models.objectstore.local import LocalObjectStore
        from outo_models.utils.paths import lfs_dir

        return LocalObjectStore(
            lfs_dir(),
            base_url=effective_base,
            presign_ttl=settings.s3_presign_ttl_seconds,
        )
    if backend == "s3":
        try:
            from outo_models.objectstore.s3 import S3ObjectStore
        except ImportError as exc:
            raise ConfigError(
                "S3 LFS 백엔드를 활성화하려면 "
                "outo_models.objectstore.s3 모듈이 필요합니다. "
                "WP-20이 아직 머지되지 않았거나 의존성이 누락되었습니다: "
                f"{exc}"
            ) from exc
        missing = [
            name
            for name, value in (
                ("OUTO_S3_ENDPOINT", settings.s3_endpoint),
                ("OUTO_S3_BUCKET", settings.s3_bucket),
                ("OUTO_S3_ACCESS_KEY", settings.s3_access_key),
                ("OUTO_S3_SECRET_KEY", settings.s3_secret_key),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "S3 LFS 백엔드가 활성화되어 있지만 다음 설정이 비어 있습니다: "
                + ", ".join(missing)
            )
        # `_S3ObjectStore` is only available in environments with WP-20
        # merged; at runtime mypy cannot verify the constructor matches
        # `ObjectStore`, so we cast.
        store: ObjectStore = cast(
            ObjectStore,
            S3ObjectStore(
                endpoint=settings.s3_endpoint,
                bucket=settings.s3_bucket,
                region=settings.s3_region,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key,
                prefix=settings.s3_prefix,
                presign_ttl=settings.s3_presign_ttl_seconds,
            ),
        )
        return store
    raise ConfigError(f"알 수 없는 OUTO_LFS_BACKEND 값: {backend!r}")


__all__ = ["create_object_store"]