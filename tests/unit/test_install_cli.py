"""Contract + behavior tests for scripts/install-cli.sh (the host CLI shim).

No podman, no root: the installer is validated via `bash -n` and by running
it unprivileged (it must refuse before touching anything). The generated
wrapper is rendered from the heredoc and exercised against a fake `podman`
on PATH to prove the image resolution order and the pull-failure guidance.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install-cli.sh"

INSTALL_TEXT = INSTALL_SCRIPT.read_text(encoding="utf-8")


class TestInstallCliScript:
    def test_bash_syntax_ok(self) -> None:
        # INSTALL_SCRIPT is a fixed repo path owned by this test, never
        # operator-supplied — S603/S607 do not apply (same pattern as
        # test_container_static.py's bash -n sweep).
        result = subprocess.run(  # noqa: S603
            ["bash", "-n", str(INSTALL_SCRIPT)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_refuses_non_root_before_writing(self) -> None:
        if os.geteuid() == 0:
            return  # CI containers may be root; the refusal path needs a non-root euid
        result = subprocess.run(  # noqa: S603
            ["bash", str(INSTALL_SCRIPT)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "sudo" in result.stderr

    def test_wrapper_contract(self) -> None:
        # The shim must mount the config dir, the data volume, and the host
        # podman socket, and must allow image overrides via OUTO_IMAGE.
        assert "/etc/outo-models:/etc/outo-models" in INSTALL_TEXT
        assert "outo-models-data:/var/lib/outo-models" in INSTALL_TEXT
        assert "podman/podman.sock" in INSTALL_TEXT
        assert "OUTO_IMAGE" in INSTALL_TEXT
        assert "OUTO_PODMAN_SOCK" in INSTALL_TEXT
        # The setup wizard needs a TTY only when stdin is a terminal.
        assert "-t 0" in INSTALL_TEXT

    def test_installer_precreates_config_dir_writable_by_container(self) -> None:
        # podman refuses to bind-mount a missing host dir (statfs error seen
        # in the field), and the in-container app (uid 1000) must be able to
        # write config.yaml when the shim runs rootless.
        assert "mkdir -p /etc/outo-models" in INSTALL_TEXT
        assert "chown 1000:1000 /etc/outo-models" in INSTALL_TEXT

    def test_default_image_points_at_stable_release(self) -> None:
        assert "ghcr.io/llaa33219/outo-models:" in INSTALL_TEXT
        assert (
            'tag="stable"' in INSTALL_TEXT
            or "tag}:-stable" in INSTALL_TEXT
            or ":-stable" in INSTALL_TEXT
        )

    def test_wrapper_resolution_order_documented(self) -> None:
        # OUTO_IMAGE env > config.yaml image key > install-time default.
        assert "OUTO_CONFIG" in INSTALL_TEXT
        assert "sed -n 's/^image:" in INSTALL_TEXT
        # Pull failure must print actionable guidance (not podman's raw error).
        assert "install-cli.sh" in INSTALL_TEXT
        assert "failed to pull" in INSTALL_TEXT


def _render_wrapper(tmp_path: Path, default_tag: str = "stable") -> Path:
    """Reproduce the wrapper the installer would write.

    Mirrors the outer heredoc: install-time `${image}` is substituted, and
    backslash-escaped `\\${...}` becomes runtime `${...}`.
    """
    body = INSTALL_TEXT.split('cat > "${dest}" <<EOF', 1)[1].split("\nEOF", 1)[0]
    default_ref = f"ghcr.io/llaa33219/outo-models:{default_tag}"
    # Install-time substitution hits only UNESCAPED `${image}` — `\${image}`
    # is runtime text in the generated wrapper (real heredocs never expand
    # escaped refs). Then undo the runtime escapes (`\$` → `$`).
    body = re.sub(r"(?<!\\)\$\{image\}", default_ref, body)
    body = body.replace("\\$", "$").replace('\\"', '"')
    wrapper = tmp_path / "outo-models"
    wrapper.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    wrapper.chmod(0o755)
    return wrapper


def _fake_podman(tmp_path: Path, *, image_exists: bool = True, pull_ok: bool = True) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "podman"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >> "$FAKE_PODMAN_LOG"\n'
        'if [[ "$1" == "image" && "$2" == "exists" ]]; then\n'
        f"    {'exit 0' if image_exists else 'exit 1'}\n"
        "fi\n"
        'if [[ "$1" == "pull" ]]; then\n'
        f"    {'exit 0' if pull_ok else 'exit 1'}\n"
        "fi\n"
        'echo "RUN $*"\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    return bin_dir


def _run_wrapper(
    wrapper: Path,
    bin_dir: Path,
    tmp_path: Path,
    *,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "FAKE_PODMAN_LOG": str(tmp_path / "podman.log"),
        "HOME": str(tmp_path),
        # No TTY in tests — the wrapper must not add -it.
        **(extra_env or {}),
    }
    return subprocess.run(  # noqa: S603
        ["bash", str(wrapper), *(args or ["status"])],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


class TestWrapperImageResolution:
    def test_default_image_when_no_config_no_env(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper, bin_dir, tmp_path, extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")}
        )
        assert result.returncode == 0, result.stderr
        assert "ghcr.io/llaa33219/outo-models:stable status" in result.stdout

    def test_config_image_key_wins_over_default(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        config = tmp_path / "config.yaml"
        config.write_text("image: ghcr.io/llaa33219/outo-models:0.2.0-dev\n", encoding="utf-8")
        result = _run_wrapper(wrapper, bin_dir, tmp_path, extra_env={"OUTO_CONFIG": str(config)})
        assert result.returncode == 0, result.stderr
        assert "ghcr.io/llaa33219/outo-models:0.2.0-dev status" in result.stdout
        assert ":stable" not in result.stdout

    def test_env_override_wins_over_config(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        config = tmp_path / "config.yaml"
        config.write_text("image: ghcr.io/llaa33219/outo-models:stable\n", encoding="utf-8")
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={
                "OUTO_CONFIG": str(config),
                "OUTO_IMAGE": "localhost/outo-models:dev",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "localhost/outo-models:dev status" in result.stdout

    def test_pull_failure_prints_guidance(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path, image_exists=False, pull_ok=False)
        result = _run_wrapper(
            wrapper, bin_dir, tmp_path, extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")}
        )
        assert result.returncode == 1
        assert "failed to pull" in result.stderr
        assert "install-cli.sh" in result.stderr
        assert "OUTO_IMAGE" in result.stderr
