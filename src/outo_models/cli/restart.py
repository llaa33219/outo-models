"""`outo-models restart` — `podman restart` the outo-models container."""

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


def restart() -> None:
    """`outo-models restart` — 컨테이너를 재시작합니다."""
    if not podman_available():
        render_error(
            ConfigError(
                "이 명령은 서버 호스트에서 실행되어야 합니다 (podman 미설치). "
                "컨테이너 배포 환경의 호스트에서 다시 실행해 주세요."
            )
        )
        raise typer_exit(1)

    rc = stream_subprocess(["podman", "restart", _CONTAINER_NAME])
    if rc != 0:
        emit_korean(f"[오류] 컨테이너 재시작 실패 (exit={rc})")
        raise typer_exit(1)
    emit_korean(f"컨테이너 재시작 완료: {_CONTAINER_NAME}")


__all__ = ["restart"]
