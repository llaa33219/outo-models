"""Spaces domain — v1 metadata + v2 container runtime.

Re-exports the public surface the server (and its tests) import:

    registry
        - `SUPPORTED_SDKS`, `DEFAULT_SDK`
        - `SpaceMeta`, `write_space_meta`, `read_space_meta`
        - `create_space`, `get_space`, `list_spaces`,
          `update_space`, `delete_space`

    runtime (v2 — Podman-backed)
        - `RuntimeState`, `RuntimeStatus`, `runtime_status`,
          `container_name_for`

    runtime_manager
        - `SpaceRuntimeManager`, `ManagedContainer`,
          `container_name`, `image_tag`

    build
        - `make_build_context`, `export_static_site`, `static_site_dir`

Container-based execution is the v2 runtime; spaces are otherwise still
treated as metadata + static exports by the proxy.
"""

from outo_models.spaces.build import (
    export_static_site,
    make_build_context,
    static_site_dir,
)
from outo_models.spaces.registry import (
    DEFAULT_SDK,
    SUPPORTED_SDKS,
    SpaceMeta,
    create_space,
    delete_space,
    get_space,
    list_spaces,
    read_space_meta,
    update_space,
    write_space_meta,
)
from outo_models.spaces.runtime import (
    RuntimeState,
    RuntimeStatus,
    container_name_for,
    runtime_status,
)
from outo_models.spaces.runtime_manager import (
    ManagedContainer,
    SpaceRuntimeManager,
    container_name,
    image_tag,
)

__all__ = [
    "DEFAULT_SDK",
    "SUPPORTED_SDKS",
    "ManagedContainer",
    "RuntimeState",
    "RuntimeStatus",
    "SpaceMeta",
    "SpaceRuntimeManager",
    "container_name",
    "container_name_for",
    "create_space",
    "delete_space",
    "export_static_site",
    "get_space",
    "image_tag",
    "list_spaces",
    "make_build_context",
    "read_space_meta",
    "runtime_status",
    "static_site_dir",
    "update_space",
    "write_space_meta",
]
