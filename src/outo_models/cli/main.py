"""`outo-models` — the operator's single CLI entry point.

Every subcommand lives in a sibling module (`setup`, `serve`, `migrate`,
`start`, `stop`, `restart`, `status`, `update`, `reset`, `admin`). This
module owns three things only:

    1. The Typer application object (`app`) — the console_script
       `outo-models = "outo_models.cli.main:app"` reads.
    2. The `--version` flag (printed from `outo_models.version`).
    3. The error funnel — every `OutoError` raised by any subcommand is
       rendered as a single Korean line + exit 1, never a traceback.

Why one fat Typer callback instead of per-command exception handlers?
    * The CLI's safety contract ("no tracebacks leak secrets") is a single
      property, easier to audit at one site than across a dozen handlers.
    * Typer 0.27's callback-decorated sub-app pattern still requires
      every command to opt in, and forgetting one command means a leaked
      traceback. One site enforces it for every command by construction.
"""

from __future__ import annotations

import typer

from outo_models import version
from outo_models.cli import render_error
from outo_models.cli.admin import admin_app
from outo_models.cli.reset import reset
from outo_models.cli.server import server_app
from outo_models.cli.setup import setup_app
from outo_models.cli.update import update
from outo_models.exceptions import OutoError

# The top-level Typer app. Name and help text are operator-visible; the
# Korean help makes the CLI self-explanatory for native speakers (AGENTS.md
# §3 — docs and CLI must agree, both in Korean).
app = typer.Typer(
    name="outo-models",
    help="outo-models 셀프 호스팅 서버 운영 CLI",
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
    pretty_exceptions_enable=False,
)


def _version_callback(value: bool) -> None:
    """Print the package version and exit when `--version` is passed."""
    if value:
        typer.echo(f"outo-models {version.__version__}")
        raise typer.Exit(code=0)


@app.callback()
def _root_callback(
    version_flag: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="패키지 버전을 출력하고 종료합니다.",
    ),
) -> None:
    """outo-models 운영 CLI 루트 콜백.

    `OutoError`는 한국어 한 줄 메시지 + exit 1로 렌더링되며 Python
    traceback은 절대 출력되지 않습니다 (AGENTS.md §2.1).
    """


app.add_typer(setup_app, name="setup", help="최초 대화형 설정 마법사")
app.add_typer(server_app, name="server", help="컨테이너 내부 서버/마이그레이션")

# Lifecycle commands are top-level Typer commands (each a single leaf
# action) so we avoid a redundant sub-app for one command. Imports are
# placed here (not at the top) to keep the import graph free of cycles —
# `start` etc. do not import the parent `app`.
from outo_models.cli.restart import restart  # noqa: E402
from outo_models.cli.start import start  # noqa: E402
from outo_models.cli.status import status  # noqa: E402
from outo_models.cli.stop import stop  # noqa: E402

app.command("start", help="outo-models 컨테이너를 시작합니다.")(start)
app.command("stop", help="outo-models 컨테이너를 중지합니다.")(stop)
app.command("restart", help="outo-models 컨테이너를 재시작합니다.")(restart)
app.command("status", help="outo-models 컨테이너 상태를 확인합니다.")(status)

app.command("update", help="이미지 갱신 + DB 마이그레이션 + 재시작")(update)
app.command("reset", help="컨테이너와 데이터를 모두 삭제 (3회 확인 게이트)")(reset)
app.add_typer(admin_app, name="admin", help="사용자 / 쿼터 / GPU 관리")


def main() -> None:
    """Console-script entry point.

    Wraps `app()` in a try/except that funnels `OutoError` into the
    project's standard renderer, so neither the operator nor a CI script
    ever sees a Python traceback for a known failure mode.
    """
    try:
        app()
    except OutoError as exc:
        render_error(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
