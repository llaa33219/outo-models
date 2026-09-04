"""Behavioral tests for assets/scripts/reset.sh against a fake podman.

Field failure being locked in: `volume rm` failed with "volume is being
used" because a leftover throwaway container still held the volume and
reset.sh only removed the container named `outo-models`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESET_SCRIPT = REPO_ROOT / "src" / "outo_models" / "assets" / "scripts" / "reset.sh"

STATE_FILE = "state.txt"


def _write_fake_podman(tmp_path: Path, *, volume_in_use: bool) -> Path:
    """A stateful fake podman: volume rm fails while a holder container exists."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / STATE_FILE
    state.write_text("holder_present=1\n" if volume_in_use else "", encoding="utf-8")
    script = bin_dir / "podman"
    script.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
state="{state}"
log="${{FAKE_PODMAN_LOG}}"
printf '%s\\n' "$*" >> "$log"

case "$1" in
  container)
    [[ "$2" == "exists" ]] && exit 0
    ;;
  volume)
    if [[ "$2" == "exists" ]]; then exit 0; fi
    if [[ "$2" == "rm" ]]; then
      if grep -q "holder_present=1" "$state"; then
        echo "Error: volume outo-models-data is being used" >&2
        exit 2
      fi
      echo "outo-models-data"
      exit 0
    fi
    ;;
  ps)
    if grep -q "holder_present=1" "$state"; then echo "abc123holder"; fi
    exit 0
    ;;
  stop)
    exit 0
    ;;
  rm)
    if [[ "$2" == "abc123holder" ]]; then
      sed -i '/holder_present/d' "$state"
    fi
    exit 0
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _run_reset(
    tmp_path: Path, bin_dir: Path, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
        "HOME": str(tmp_path),
        **(extra_env or {}),
    }
    return subprocess.run(  # noqa: S603 — fixed repo script path
        ["bash", str(RESET_SCRIPT)],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class TestResetScript:
    def test_removes_volume_holders_before_volume_rm(self, tmp_path: Path) -> None:
        bin_dir = _write_fake_podman(tmp_path, volume_in_use=True)
        result = _run_reset(tmp_path, bin_dir)
        assert result.returncode == 0, result.stderr
        log = (tmp_path / "podman.log").read_text(encoding="utf-8").splitlines()
        holder_rm = next(i for i, line in enumerate(log) if line == "rm abc123holder")
        volume_rm = next(i for i, line in enumerate(log) if line == "volume rm outo-models-data")
        assert holder_rm < volume_rm, log
        assert "first-install state" in result.stdout

    def test_never_removes_itself(self, tmp_path: Path) -> None:
        """Field failure: the shim's own CLI container holds the volume, and
        sweeping it kills the reset mid-script (self SIGKILL). The holder list
        must exclude our own container id (the hostname)."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (tmp_path / STATE_FILE).write_text("holder_present=1\n", encoding="utf-8")
        # The self id lives ONLY in our fake cgroup file (hostname does not
        # match — the --network=host failure mode from the field).
        self_id = "a" * 64
        cgroup = tmp_path / "cgroup"
        cgroup.write_text(f"0::/machine.slice/libpod-{self_id}.scope\n", encoding="utf-8")
        script = bin_dir / "podman"
        script.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
state="{tmp_path / STATE_FILE}"
printf '%s\\n' "$*" >> "${{FAKE_PODMAN_LOG}}"
case "$1" in
  container) [[ "$2" == "exists" ]] && exit 1 ;;  # no named container
  volume)
    [[ "$2" == "exists" ]] && exit 0
    if [[ "$2" == "rm" ]]; then
      if grep -q "holder_present=1" "$state"; then
        echo "Error: volume is being used" >&2
        exit 2
      fi
      exit 0
    fi
    ;;
  ps)
    printf '%s\\n' "{self_id}" "deadbeefcafe1234ffffffffffffffffffffffffffffffffffff"
    exit 0
    ;;
  stop) exit 0 ;;
  rm)
    if [[ "$2" == "{self_id}"* ]]; then
      echo "SELF-KILL ATTEMPT" >&2
      exit 9
    fi
    if [[ "$2" == "deadbeef"* ]]; then
      sed -i '/holder_present/d' "$state"
    fi
    exit 0
    ;;
esac
exit 0
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        result = _run_reset(tmp_path, bin_dir, extra_env={"OUTO_SELF_CGROUP_FILE": str(cgroup)})
        assert result.returncode == 0, result.stderr
        log = (tmp_path / "podman.log").read_text(encoding="utf-8")
        assert "SELF-KILL ATTEMPT" not in result.stderr
        assert f"rm {self_id}" not in log
        assert "rm deadbeefcafe1234ffffffffffffffffffffffffffffffffffff" in log
        assert "first-install state" in result.stdout

    def test_plain_path_without_holders(self, tmp_path: Path) -> None:
        bin_dir = _write_fake_podman(tmp_path, volume_in_use=False)
        result = _run_reset(tmp_path, bin_dir)
        assert result.returncode == 0, result.stderr
        assert "removing data volume" in result.stdout

    def test_no_containers_no_volume_is_noop(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        script = bin_dir / "podman"
        script.write_text(
            '#!/usr/bin/env bash\nif [[ "$2" == "exists" ]]; then exit 1; fi\nexit 0\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        result = _run_reset(tmp_path, bin_dir)
        assert result.returncode == 0, result.stderr
        assert "skipping" in result.stdout
