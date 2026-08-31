"""`outo-models stop` — `podman stop` the outo-models container.

Symmetrical with `start.py`: when podman is absent the command prints a
Korean host-not-available message and exits 1 (we treat this as a real
error — `start/stop/restart` all require the server host).
"""

from __future__ import annotations

from outo_models.cli import (
    emit_korean,
    podman_available,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.exceptions import ConfigError

_CONTAINER_NAME = "outo-models"


def stop() -> None:
    """`outo-models stop` — 컨테이너를 중지합니다."""
    if not podman_available():
        render_error(
            ConfigError(
                "이 명령은 서버 호스트에서 실행되어야 합니다 (podman 미설치). "
                "컨테이너 배포 환경의 호스트에서 다시 실행해 주세요."
            )
        )
        raise typer_exit(1)

    rc = stream_subprocess(["podman", "stop", _CONTAINER_NAME])
    if rc != 0:
        emit_korean(f"[오류] 컨테이너 중지 실패 (exit={rc})")
        raise typer_exit(1)
    emit_korean(f"컨테이너 중지 완료: {_CONTAINER_NAME}")


__all__ = ["stop"]
