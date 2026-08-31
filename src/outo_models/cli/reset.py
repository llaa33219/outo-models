"""`outo-models reset` — destructive cleanup, gated by the triple-confirm rule.

AGENTS.md §2.2 — the triple-yes gate is inviolable. This module is the
*only* code path allowed to delete container / volume / local data, and
it implements the rule exactly as the spec demands:

    * Default (no `--destroy`) → DRY RUN. Print what would be destroyed
      (user count, repo count, total bytes, volume name) and exit 0 with
      a Korean reminder to re-run with `--destroy`.
    * `--destroy` requires `OUTO_DESTRUCTIVE=1` in the environment. Without
      it → refusal message, exit 1.
    * `OUTO_DESTRUCTIVE=1` without `--destroy` → still DRY RUN.
    * Both present → three prompts in succession, each printing an
      escalating summary. The literal answer `yes` (no trailing whitespace,
      no caps, no Korean, no Y/N shortcut) is the ONLY string that counts;
      anything else aborts with exit 1.
    * On a non-interactive stdin (EOF before any prompt completes) the
      command must abort safely with exit 1 — never default to "yes".
    * Only after all three are entered exactly does the command:
        1. Run `container/scripts/reset.sh` (host-side container wipe).
        2. Wipe the local `data_dir` (dev installs without podman).

The dry-run summary is computed against the local DB (the operator runs
this on the server host); an empty database simply prints zeros, which
is the expected behavior for a fresh install that has been "reset".
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console
from sqlalchemy import func, select

from outo_models.cli import (
    container_script,
    format_bytes,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.config import get_settings
from outo_models.db import Repo, User, get_engine, get_session_factory
from outo_models.exceptions import ConfigError, OutoError

# Env var the triple-yes gate requires. Same convention as `OUTO_CONFIG`
# etc. Documented in `docs/cli.md` (operator-facing) and `docs/security.md`
# (rationale).
_DESTRUCTIVE_ENV = "OUTO_DESTRUCTIVE"

# The literal token that counts as "yes" — anything else aborts.
_YES_TOKEN = "yes"  # noqa: S105 — keyword, not a password

# How many times the operator must type exactly `yes` for the gate to open.
# Bumping this number requires changing `AGENTS.md §2.2` first; tests assert
# on this exact value so an accidental change fails CI immediately.
_REQUIRED_YES_COUNT = 3

# Container / volume names match `container/scripts/reset.sh`. They are
# duplicated here only so the dry-run summary can render the planned
# destruction without spawning the script.
_CONTAINER_NAME = "outo-models"
_VOLUME_NAME = "outo-models-data"


def reset(
    destroy: bool = typer.Option(
        False,
        "--destroy",
        help="실제 삭제를 수행합니다 (게이트 통과 필요). 기본은 dry-run.",
    ),
) -> None:
    """`outo-models reset` — 모든 데이터를 삭제 (3회 확인 게이트).

    기본 동작은 dry-run: 삭제될 대상 요약을 출력하고 0 으로 종료합니다.
    실제 삭제하려면 `--destroy` 와 환경변수 `OUTO_DESTRUCTIVE=1` 을
    함께 사용하고, 세 번 정확히 `yes` 를 입력해야 합니다.
    """
    _reset_impl(destroy=destroy)


async def _compute_summary() -> tuple[int, int, int, str]:
    """Return `(user_count, repo_count, total_bytes, volume_name)`.

    `total_bytes` sums the on-disk size of every file under
    `data_dir / "repos"`. If the engine / schema is missing the dry-run
    prints zeros (a brand-new install reset should not crash).
    """
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    repos_dir = data_dir / "repos"

    user_count = 0
    repo_count = 0
    total_bytes = 0

    try:
        engine = get_engine(settings)
        factory = get_session_factory(engine)
        async with factory() as session:
            user_count = (
                await session.execute(select(func.count()).select_from(User))
            ).scalar_one()
            repo_count = (
                await session.execute(select(func.count()).select_from(Repo))
            ).scalar_one()
    except Exception:  # noqa: S110 — dry-run must never crash on DB errors
        # Schema not migrated yet, engine not reachable, etc. → print zeros.
        pass

    if repos_dir.exists():
        for path in repos_dir.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    continue

    return int(user_count), int(repo_count), int(total_bytes), _VOLUME_NAME


def _print_dry_run(user_count: int, repo_count: int, total_bytes: int, volume: str) -> None:
    """Print the would-be-destroyed summary in Korean."""
    console = Console()
    console.print(
        "[bold yellow][dry-run] 다음 데이터가 삭제됩니다 (실제 삭제는 수행하지 않음):[/bold yellow]"
    )
    console.print(f"  - 사용자 수: {user_count}")
    console.print(f"  - 저장소 수: {repo_count}")
    console.print(f"  - 디스크 사용량: {format_bytes(total_bytes)}")
    console.print(f"  - 컨테이너: {_CONTAINER_NAME}")
    console.print(f"  - 볼륨: {volume}")
    console.print()
    console.print(
        "실제로 삭제하려면 [bold]--destroy[/bold] 옵션과 "
        f"[bold]{_DESTRUCTIVE_ENV}=1[/bold] 환경변수를 함께 사용하세요."
    )


def _print_escalation_warning(
    stage: int, user_count: int, repo_count: int, total_bytes: int
) -> None:
    """Print the escalating warning shown above each `yes` prompt."""
    console = Console(stderr=True)
    summaries = {
        1: (
            f"[정말로 삭제하시겠습니까?] 사용자 {user_count}명, "
            f"저장소 {repo_count}개, {format_bytes(total_bytes)}."
        ),
        2: "[경고] 이 작업은 되돌릴 수 없습니다 (복구 불가). 모든 데이터가 영구히 사라집니다.",
        3: (
            f"[최종 확인] 컨테이너 '{_CONTAINER_NAME}' 와 볼륨 '{_VOLUME_NAME}', "
            f"로컬 데이터 디렉터리가 모두 삭제됩니다. {user_count}명의 사용자와 "
            f"{repo_count}개의 저장소가 사라집니다."
        ),
    }
    console.print(f"\n[bold red]{summaries[stage]}[/bold red]")


def _gather_yes_confirmations(user_count: int, repo_count: int, total_bytes: int) -> bool:
    """Run the triple-yes gate; return True iff every prompt accepted `yes`.

    A non-interactive stdin (EOF) aborts safely — the builtin `input()`
    raises `EOFError` in that case, which we catch and translate into a
    structured refusal (no default-to-yes surprise).
    """
    console = Console(stderr=True)
    for stage in range(1, _REQUIRED_YES_COUNT + 1):
        _print_escalation_warning(stage, user_count, repo_count, total_bytes)
        # `input()` (not `rich.prompt.Confirm`) so the answer must be
        # exactly `yes` — `Confirm.ask` would silently accept `y` and
        # weaken the AGENTS.md §2.2 gate.
        try:
            answer = input(f"[{stage}/{_REQUIRED_YES_COUNT}] 정확히 '{_YES_TOKEN}' 입력: ")
        except EOFError:
            console.print("[bold red]입력 스트림이 닫혔습니다. 작업을 중단합니다.[/bold red]")
            return False
        # `answer != _YES_TOKEN` — no `.strip()`, so `yes ` (trailing
        # whitespace) is rejected. The spec demands exact match; an
        # operator who truly meant `yes` types it without trailing space.
        if answer != _YES_TOKEN:
            console.print(
                f"[bold red]'{_YES_TOKEN}'이(가) 아닙니다 — 작업을 중단합니다.[/bold red]"
            )
            return False
    return True


def _wipe_local_data_dir() -> None:
    """Remove `data_dir` from a dev install (no podman on this host).

    This is a separate step from the container wipe because dev installs
    have a `data_dir` under pytest's tmp_path (or the operator's checkout),
    not inside a podman volume.
    """
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    if not data_dir.exists():
        return
    try:
        shutil.rmtree(data_dir)
    except OSError as exc:
        raise OutoError(
            f"로컬 데이터 디렉터리 삭제 실패 ({data_dir}): {exc}",
            code="reset_local_wipe_failed",
        ) from exc


def _reset_impl(destroy: bool) -> None:
    """Top-level handler — split out so tests can call it directly."""
    summary = asyncio.run(_compute_summary())
    user_count, repo_count, total_bytes, volume = summary

    env_destructive = os.environ.get(_DESTRUCTIVE_ENV) == "1"

    if not destroy:
        _print_dry_run(user_count, repo_count, total_bytes, volume)
        if env_destructive:
            note = (
                f"\n[yellow]참고: {_DESTRUCTIVE_ENV}=1 이 설정되어 있지만 "
                "--destroy 가 없어 dry-run 으로 종료합니다.[/yellow]"
            )
            Console().print(note)
        asyncio.run(_dispose_engines_safe())
        raise typer_exit(0)

    if not env_destructive:
        asyncio.run(_dispose_engines_safe())
        render_error(
            ConfigError(
                f"--destroy 를 사용하려면 환경변수 {_DESTRUCTIVE_ENV}=1 이 필요합니다.",
                code="reset_env_missing",
            )
        )
        raise typer_exit(1)

    if not _gather_yes_confirmations(user_count, repo_count, total_bytes):
        asyncio.run(_dispose_engines_safe())
        render_error(OutoError("확인 게이트를 통과하지 못했습니다.", code="reset_aborted"))
        raise typer_exit(1)

    script = container_script("reset.sh")
    rc = stream_subprocess(["bash", script])
    if rc != 0:
        asyncio.run(_dispose_engines_safe())
        render_error(OutoError(f"reset.sh 가 실패했습니다 (exit={rc})", code="reset_script_failed"))
        raise typer_exit(1)

    try:
        _wipe_local_data_dir()
    except OutoError as exc:
        asyncio.run(_dispose_engines_safe())
        render_error(exc)
        raise typer_exit(1) from exc

    Console().print(
        "[bold green][완료] outo-models 가 초기 설치 상태로 되돌아갔습니다.[/bold green]"
    )
    Console().print("다시 시작하려면 `outo-models setup` 을 실행해 주세요.")


async def _dispose_engines_safe() -> None:
    """Best-effort engine teardown for the reset path.

    `_compute_summary` opens the engine in its own `asyncio.run()` cycle,
    so the aiosqlite worker threads belong to a closed loop when
    `_reset_impl` returns. `dispose_engines()` against those threads
    raises `RuntimeError: Event loop is closed`; we swallow that because
    reset is read-only against the DB — the next command will rebuild a
    fresh engine.
    """
    import contextlib

    from outo_models.db.engine import dispose_engines

    engine = get_engine()
    with contextlib.suppress(Exception):
        await engine.dispose()
    with contextlib.suppress(Exception):
        await dispose_engines()


__all__ = ["reset"]
