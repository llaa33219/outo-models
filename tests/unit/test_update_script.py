"""Behavior tests for `assets/scripts/update.sh` — the host-side update flow.

No podman on this machine (AGENTS.md §4), so the script is driven against a
fake `podman` on PATH that records every argv it receives. That pins the
ARGUMENT CONTRACT, which is what the update flow actually is:

    1. `pull <image>`
    2. `run --rm ... <image> server migrate`   (throwaway container)
    3. `run --rm ... <image> start`            (recreate, NEW image's CLI)

Step 3 is `start`, not `podman restart`: a restart reuses the OLD container
spec, so spec changes shipped by the new image (v0.6.6 added the Podman
socket mount) never applied and left Spaces container SDKs reporting
"Cannot connect to the Podman socket". `start` replaces the container with the
current spec, so the recreate IS `start`.

The recreate invocation must mirror the host CLI shim
(scripts/install-cli.sh), otherwise `start` cannot see config.yaml or the
Podman socket and cannot mount the socket into the new server container.
"""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SCRIPT = REPO_ROOT / "src" / "outo_models" / "assets" / "scripts" / "update.sh"

IMAGE = "ghcr.io/llaa33219/outo-models:stable"
NEW_IMAGE = "ghcr.io/llaa33219/outo-models:0.7.0-stable"
MIGRATE = "server migrate"
START = "start"


def _fake_podman(bin_dir: Path) -> None:
    """Install a `podman` that logs argv (NUL-separated per call) and exits.

    `FAKE_PODMAN_FAIL` holds a substring; any invocation containing it exits
    non-zero, which is how the failure paths are exercised.
    """
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "podman"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\0\' "$@" >> "$FAKE_PODMAN_LOG"\n'
        "printf '\\n' >> \"$FAKE_PODMAN_LOG\"\n"
        'if [[ -n "${FAKE_PODMAN_FAIL:-}" && "$*" == *"${FAKE_PODMAN_FAIL}"* ]]; then\n'
        '    echo "fake podman: forced failure on ${FAKE_PODMAN_FAIL}" >&2\n'
        "    exit 125\n"
        "fi\n"
        'echo "podman $*"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)


def _run(
    tmp_path: Path, *argv: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    _fake_podman(tmp_path / "bin")
    return subprocess.run(  # noqa: S603 — fixed argv: this file's script path
        ["bash", str(UPDATE_SCRIPT), *argv],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
            "HOME": str(tmp_path),
            **(env or {}),
        },
    )


def _invocations(tmp_path: Path) -> list[list[str]]:
    """Every recorded podman invocation, in order, as argv lists."""
    log = tmp_path / "podman.log"
    if not log.is_file():
        return []
    return [
        [arg for arg in line.split("\0") if arg]
        for line in log.read_text(encoding="utf-8").splitlines()
    ]


def _recreate(invocations: list[list[str]]) -> list[str]:
    """The step-3 invocation (the one whose last arg is the `start` command)."""
    return next((inv for inv in invocations if inv[-1:] == [START]), [])


@pytest.fixture
def host_socket(tmp_path: Path) -> str:
    """A real socket file — `-S` checks in the script need a live listener."""
    with socket.socket(socket.AF_UNIX) as listener:
        path = tmp_path / "host.sock"
        listener.bind(str(path))
        return str(path)


def _update(tmp_path: Path, image: str = IMAGE, **env: str) -> subprocess.CompletedProcess[str]:
    return _run(tmp_path, image, env=env)


class TestUpdateStepSequence:
    def test_pull_then_migrate_then_recreate(self, tmp_path: Path) -> None:
        # Given / When
        result = _update(tmp_path)
        # Then
        assert result.returncode == 0, result.stderr
        calls = _invocations(tmp_path)
        # `server migrate` is two argv entries (the entrypoint prepends the CLI name).
        assert calls[0][-2:] == ["pull", IMAGE]
        assert calls[1][-2:] == ["server", "migrate"]
        assert calls[2][-1:] == [START]
        assert len(calls) == 3

    def test_recreate_never_restarts_the_container(self, tmp_path: Path) -> None:
        # `podman restart` reuses the OLD spec — the exact field failure.
        assert _update(tmp_path).returncode == 0
        assert [call for call in _invocations(tmp_path) if "restart" in call] == []

    def test_recreate_runs_the_new_image_cli(self, tmp_path: Path) -> None:
        # The container that launched `update` may still carry the previous
        # release's code; the recreate must run the image just pulled.
        assert _update(tmp_path, NEW_IMAGE).returncode == 0
        assert _recreate(_invocations(tmp_path))[-2] == NEW_IMAGE

    def test_recreate_mirrors_the_cli_shim_mount_set(
        self, tmp_path: Path, host_socket: str
    ) -> None:
        # Given / When: same mounts the shim gives `outo-models start`.
        assert _update(tmp_path, OUTO_PODMAN_SOCK=host_socket).returncode == 0
        # Then
        call = _recreate(_invocations(tmp_path))
        assert "--rm" in call
        assert "--network=host" in call
        assert "--userns=keep-id" in call
        assert "/etc/outo-models:/etc/outo-models" in call
        assert "outo-models-data:/var/lib/outo-models" in call
        assert f"{host_socket}:/run/podman/podman.sock" in call
        # OUTO_PODMAN_SOCKET = in-container path the CLI connects through;
        # OUTO_PODMAN_SOCK = HOST path `start` mounts into the server container.
        assert "OUTO_PODMAN_SOCKET=/run/podman/podman.sock" in call
        assert f"OUTO_PODMAN_SOCK={host_socket}" in call

    def test_recreate_probes_the_rootless_socket_without_the_env(
        self, tmp_path: Path, host_socket: str
    ) -> None:
        # A host-shell `bash update.sh` has no OUTO_PODMAN_SOCK; the shim's
        # rootless-first probe must still find the socket, or the recreated
        # container comes up without one.
        runtime_dir = tmp_path / "run"
        (runtime_dir / "podman").mkdir(parents=True)
        (runtime_dir / "podman" / "podman.sock").symlink_to(host_socket)
        result = _update(tmp_path, XDG_RUNTIME_DIR=str(runtime_dir))
        assert result.returncode == 0, result.stderr
        call = _recreate(_invocations(tmp_path))
        rootless = f"{runtime_dir}/podman/podman.sock"
        assert f"{rootless}:/run/podman/podman.sock" in call
        assert f"OUTO_PODMAN_SOCK={rootless}" in call

    def test_recreate_survives_a_host_without_a_socket(self, tmp_path: Path) -> None:
        # No socket anywhere: the run must still be attempted (podman may be
        # reachable another way) rather than aborting the update.
        result = _update(tmp_path, XDG_RUNTIME_DIR=str(tmp_path / "absent"))
        assert result.returncode == 0, result.stderr
        call = _recreate(_invocations(tmp_path))
        assert call, "the recreate must still run"
        assert not any("podman.sock" in arg for arg in call)

    def test_running_twice_repeats_the_same_recreate(self, tmp_path: Path) -> None:
        # Idempotent: `start` no-ops on an already-current container, so a
        # second `update` must reach the same end state, not fail.
        assert _update(tmp_path).returncode == 0
        first = _recreate(_invocations(tmp_path))
        assert first, "the first update must recreate the container"
        (tmp_path / "podman.log").unlink()
        assert _update(tmp_path).returncode == 0
        assert _recreate(_invocations(tmp_path)) == first


class TestInContainerSocketResolution:
    """`OUTO_PODMAN_SOCK` is the only trustworthy socket path inside a container.

    A host shim installed before v0.6.6 mounts the host socket at
    /run/podman/podman.sock but never forwards the HOST path. The socket probe
    then matches that container-side mount point and hands it to podman as a
    `-v` SOURCE, and the host engine dies with
    "statfs /run/podman/podman.sock: permission denied" (exit 125) — a cryptic
    crash that blocked `outo-models update` outright. The fix is a one-glance
    instruction instead: refresh the shim, re-run.
    """

    def test_in_container_without_forwarded_host_socket_stops_before_recreate(
        self, tmp_path: Path
    ) -> None:
        # Given / When: `container` is the marker podman exports in-container;
        # OUTO_PODMAN_SOCK is absent, i.e. the stale shim.
        result = _update(tmp_path, container="podman")
        # Then: guidance, not a statfs crash.
        assert result.returncode != 0
        assert "install-cli.sh" in result.stderr
        assert "outo-models update" in result.stderr
        calls = _invocations(tmp_path)
        assert calls[0][-2:] == ["pull", IMAGE]
        assert calls[1][-2:] == ["server", "migrate"]
        # The nested `start` never ran — the running server container is untouched.
        assert _recreate(calls) == []

    def test_in_container_with_forwarded_host_socket_recreates(
        self, tmp_path: Path, host_socket: str
    ) -> None:
        # Given / When: the normal shim path — inside a container, but the HOST
        # socket path is forwarded.
        result = _update(tmp_path, container="podman", OUTO_PODMAN_SOCK=host_socket)
        # Then
        assert result.returncode == 0, result.stderr
        call = _recreate(_invocations(tmp_path))
        assert f"{host_socket}:/run/podman/podman.sock" in call
        assert f"OUTO_PODMAN_SOCK={host_socket}" in call


class TestUpdateFailureSurfacing:
    def test_pull_failure_stops_before_migrating(self, tmp_path: Path) -> None:
        result = _update(tmp_path, FAKE_PODMAN_FAIL="pull")
        assert result.returncode != 0
        assert [call[0] for call in _invocations(tmp_path)] == ["pull"]

    def test_migrate_failure_stops_before_recreate(self, tmp_path: Path) -> None:
        # A failed migration must never be followed by a container swap.
        result = _update(tmp_path, FAKE_PODMAN_FAIL=MIGRATE)
        assert result.returncode != 0
        assert _recreate(_invocations(tmp_path)) == []

    def test_recreate_failure_is_reported(self, tmp_path: Path) -> None:
        result = _update(tmp_path, FAKE_PODMAN_FAIL=START)
        assert result.returncode != 0
        assert "forced failure" in result.stderr

    def test_missing_podman_exits_zero(self, tmp_path: Path) -> None:
        # Graceful no-op: `update.sh` must never hard-fail a non-podman host.
        # PODMAN_BIN names a binary that cannot exist so the lookup fails on
        # every host: runners may ship a real /usr/bin/podman (CI does).
        result = _run(
            tmp_path,
            IMAGE,
            env={
                "PODMAN_BIN": "outo-models-no-such-podman",
                "PATH": "/usr/bin:/bin",
                "FAKE_PODMAN_LOG": str(tmp_path / "x.log"),
            },
        )
        assert result.returncode == 0, result.stderr
        assert "podman is not installed" in result.stdout
