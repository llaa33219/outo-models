"""Static contract tests for scripts/install-cli.sh (the host CLI shim).

No podman, no root: the script is validated via `bash -n` and by running it
unprivileged (it must refuse before touching anything).
"""

from __future__ import annotations

import os
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

    def test_default_image_points_at_stable_release(self) -> None:
        assert "ghcr.io/llaa33219/outo-models:" in INSTALL_TEXT
        assert (
            'tag="stable"' in INSTALL_TEXT
            or "tag}:-stable" in INSTALL_TEXT
            or ":-stable" in INSTALL_TEXT
        )
