"""`outo-models stop` — `podman stop` the outo-models container.

Symmetrical with `start.py`: when podman is absent the command prints a
host-not-available message and exits 1 (we treat this as a real error —
`start/stop/restart` all require the server host).
"""

from __future__ import annotations

from outo_models.cli import (
    podman_available,
    print_status,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.exceptions import ConfigError

_CONTAINER_NAME = "outo-models"


def stop() -> None:
    """`outo-models stop` — stop the container."""
    if not podman_available():
        render_error(
            ConfigError(
                "this command must be run on the server host (podman not installed). "
                "Re-run it on the host of the deployed container."
            )
        )
        raise typer_exit(1)

    rc = stream_subprocess(["podman", "stop", _CONTAINER_NAME])
    if rc != 0:
        print_status(f"[error] container failed to stop (exit={rc})")
        raise typer_exit(1)
    print_status(f"container stopped: {_CONTAINER_NAME}")


__all__ = ["stop"]
