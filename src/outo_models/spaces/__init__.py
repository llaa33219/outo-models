"""Spaces domain — v1 metadata layer.

Re-exports the public surface WP-13 (routers) and WP-14 (templates)
import:

    registry
        - `SUPPORTED_SDKS`, `DEFAULT_SDK`
        - `SpaceMeta`, `write_space_meta`, `read_space_meta`
        - `create_space`, `get_space`, `list_spaces`,
          `update_space`, `delete_space`

    runtime (v1 stub)
        - `RuntimeState`, `RuntimeStatus`, `runtime_status`

Container-based execution is a v2 roadmap item; today every space is
treated as a metadata-only object that points at a static page.
"""

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
from outo_models.spaces.runtime import RuntimeState, RuntimeStatus, runtime_status

__all__ = [
    "DEFAULT_SDK",
    "SUPPORTED_SDKS",
    "RuntimeState",
    "RuntimeStatus",
    "SpaceMeta",
    "create_space",
    "delete_space",
    "get_space",
    "list_spaces",
    "read_space_meta",
    "runtime_status",
    "update_space",
    "write_space_meta",
]