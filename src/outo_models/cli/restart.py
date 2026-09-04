"""`outo-models restart` — `podman restart` the outo-models container."""

from __future__ import annotations

from outo_models.cli import (
    podman_available,
    podman_base,
    print_status,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.exceptions import ConfigError

_CONTAINER_NAME = "outo-models"


def restart() -> None:
    """`outo-models restart` — restart the container."""
    if not podman_available():
        render_error(
            ConfigError(
                "this command must be run on the server host (podman not installed). "
                "Re-run it on the host of the deployed container."
            )
        )
        raise typer_exit(1)

    rc = stream_subprocess([*podman_base(), "restart", _CONTAINER_NAME])
    if rc != 0:
        print_status(f"[error] container failed to restart (exit={rc})")
        raise typer_exit(1)
    print_status(f"container restarted: {_CONTAINER_NAME}")


__all__ = ["restart"]
