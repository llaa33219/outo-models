"""`outo-models update` — host-side script wrapper.

Delegates to `container/scripts/update.sh` (resolved via
`container_script()`) which performs:

    1. `podman pull <image-tag>`
    2. `podman run --rm ... outo-models migrate` (throwaway container)
    3. `podman restart outo-models` (if the container is running)

The CLI's only job is to locate the script, build its argv, stream its
output, and translate its exit code into a CLI exit. The wizard and the
update path both speak the same script, so the safety properties of the
update flow live in `update.sh`, not in this file.
"""

from __future__ import annotations

import typer

from outo_models.cli import (
    container_script,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.exceptions import OutoError


def update(
    image: str = typer.Option(
        "outo-models:stable",
        "--image",
        help="Image tag to update to.",
    ),
) -> None:
    """`outo-models update` — pull the new image, run DB migrations, restart."""
    script = container_script("update.sh")
    argv = ["bash", script, image]
    rc = stream_subprocess(argv)
    if rc != 0:
        render_error(
            OutoError(
                f"update script failed (exit={rc})",
                code="update_failed",
            )
        )
        raise typer_exit(1)


__all__ = ["update"]
