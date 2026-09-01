"""`outo-models status` — print whether the outo-models container is running.

Unlike `start` / `stop` / `restart`, this command exits 0 even when
podman is absent. The reasoning: a status check on a development machine
that simply does not run containers is informational, not an operational
failure — the operator did nothing wrong, the host is just not the server.
CI scripts that gate on this command can grep the output instead of
inspecting the exit code.
"""

from __future__ import annotations

import shutil
import subprocess

from outo_models.cli import print_status

_CONTAINER_NAME = "outo-models"


def status() -> None:
    """`outo-models status` — print the container run state."""
    if shutil.which("podman") is None:
        print_status("[info] podman is not installed on this host (development environment).")
        return

    # `podman container exists` returns 0 when present, 1 when absent.
    exists = subprocess.run(  # noqa: S603
        ["podman", "container", "exists", _CONTAINER_NAME],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        print_status(f"[status] no such container: {_CONTAINER_NAME}")
        return

    inspect = subprocess.run(  # noqa: S603
        ["podman", "inspect", "--format", "{{.State.Running}}", _CONTAINER_NAME],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    is_running = inspect.stdout.strip().lower() == "true"
    if is_running:
        print_status(f"[status] running: {_CONTAINER_NAME}")
        return
    print_status(f"[status] stopped: {_CONTAINER_NAME}")


__all__ = ["status"]
