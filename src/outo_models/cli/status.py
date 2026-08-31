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

from outo_models.cli import emit_korean

_CONTAINER_NAME = "outo-models"


def status() -> None:
    """`outo-models status` — 컨테이너 실행 상태를 출력합니다."""
    if shutil.which("podman") is None:
        emit_korean("[정보] 이 호스트에는 podman이 설치되어 있지 않습니다 (개발 환경).")
        return

    # `podman container exists` returns 0 when present, 1 when absent.
    exists = subprocess.run(  # noqa: S603
        ["podman", "container", "exists", _CONTAINER_NAME],  # noqa: S607
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        emit_korean(f"[상태] 컨테이너 없음: {_CONTAINER_NAME}")
        return

    inspect = subprocess.run(  # noqa: S603
        ["podman", "inspect", "--format", "{{.State.Running}}", _CONTAINER_NAME],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    is_running = inspect.stdout.strip().lower() == "true"
    if is_running:
        emit_korean(f"[상태] 실행 중: {_CONTAINER_NAME}")
        return
    emit_korean(f"[상태] 중지됨: {_CONTAINER_NAME}")


__all__ = ["status"]
