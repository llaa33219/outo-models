"""Pluggable LFS object storage.

Public surface:
    - `LfsAction` / `ObjectStore` — the protocol WP-20's S3 backend
      implements against. Locked by `outo_models.objectstore.base`.
    - `LocalObjectStore` — the filesystem-backed implementation.
    - `create_object_store(settings)` — the factory that picks the right
      backend from `Settings.lfs_backend`.
"""

from outo_models.objectstore.base import LfsAction, ObjectStore
from outo_models.objectstore.factory import create_object_store
from outo_models.objectstore.local import LocalObjectStore

__all__ = ["LfsAction", "LocalObjectStore", "ObjectStore", "create_object_store"]