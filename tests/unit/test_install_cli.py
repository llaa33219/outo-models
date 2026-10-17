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

# Marker used to delimit the firewall-script heredoc inside the installer.
# Quoted on the bash side so no expansion happens while writing the file.
_FIREWALL_HEREDOC_START = "cat > \"${firewall_dest}\" <<'OUTO_FIREWALL_HEREDOC_END'"
_FIREWALL_HEREDOC_END = "OUTO_FIREWALL_HEREDOC_END"
_LOW_PORTS_HEREDOC_START = "cat > \"${low_ports_dest}\" <<'OUTO_LOW_PORTS_HEREDOC_END'"
_LOW_PORTS_HEREDOC_END = "OUTO_LOW_PORTS_HEREDOC_END"


def extract_embedded(text: str, start_marker: str, end_marker: str) -> str:
    """Pull an embedded script body out of the installer's heredoc.

    Returns the bytes between the start marker and the closing terminator,
    with a single trailing newline appended so it compares byte-for-byte
    against the on-disk file (the bash heredoc preserves the newline before
    its terminator, and the file has a trailing newline too).
    """
    start_idx = text.find(start_marker)
    assert start_idx != -1, f"heredoc marker not found in installer: {start_marker}"
    body_start = start_idx + len(start_marker) + 1  # skip the trailing newline
    end_idx = text.find(end_marker, body_start)
    assert end_idx != -1, f"heredoc terminator not found in installer: {end_marker}"
    body = text[body_start:end_idx]
    # Real bash heredocs leave the line BEFORE the terminator intact and the
    # terminator itself flushes the buffer without a trailing newline. We
    # append one to match the on-disk file (which ends with a newline).
    if not body.endswith("\n"):
        body += "\n"
    return body


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
        # Host CDI spec directories: forwarded verbatim when the operator sets
        # them, probed on the host otherwise (see the dedicated test below).
        assert "OUTO_CDI_DIRS" in INSTALL_TEXT
        # The setup wizard needs a TTY only when stdin is a terminal.
        assert "-t 0" in INSTALL_TEXT
        # keep-id keeps volume/config ownership on the invoking host user
        # (rootless subuid mapping otherwise breaks writes — field failure).
        assert "--userns=keep-id" in INSTALL_TEXT

    def test_wrapper_forwards_host_socket_path_into_cli_container(self) -> None:
        # `outo-models start` runs in a second container and needs the HOST
        # socket path to mount; it travels as OUTO_PODMAN_SOCK (never
        # OUTO_PODMAN_SOCKET, which means the in-container path).
        body = _wrapper_text()
        assert '-e OUTO_PODMAN_SOCK="${sock}"' in body
        assert '-v "${sock}:/run/podman/podman.sock"' in body

    def test_socket_probe_is_rootless_first(self) -> None:
        # The server container runs uid 1000 and cannot connect to a rootful
        # root:root 0660 socket, so the rootless user socket must win.
        body = _wrapper_text()
        user_probe = body.index(
            'user_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"'
        )
        assert user_probe < body.index("elif [[ -S /run/podman/podman.sock ]]")

    def test_wrapper_probes_host_cdi_dirs_and_forwards_them(self) -> None:
        # The podman<6 GPU fallback reads the host CDI specs inside the server
        # container, and `start` cannot probe them: it runs in a container where
        # every visible path is the container's own mount point. The shim runs
        # ON THE HOST, so it probes and forwards the host paths as
        # OUTO_CDI_DIRS — the same host-truth channel as OUTO_PODMAN_SOCK.
        body = _wrapper_text()
        assert 'cdi_dirs="${OUTO_CDI_DIRS:-}"' in body
        assert "for dir in /etc/cdi /var/run/cdi" in body
        assert 'if [[ -d "${dir}" ]]' in body
        assert 'args+=(-e "OUTO_CDI_DIRS=${cdi_dirs}")' in body

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

    def test_installs_firewall_script_to_host_share(self) -> None:
        # The wizard spawns this exact path on the host; a single-file
        # `curl | sudo bash` install must place it there.
        assert "/usr/local/share/outo-models/firewall-open.sh" in INSTALL_TEXT
        assert "firewall_dest=" in INSTALL_TEXT
        assert 'chmod 0755 "${firewall_dest}"' in INSTALL_TEXT

    def test_embedded_firewall_script_matches_repo_byte_for_byte(self) -> None:
        # Drift guard: a change to either file alone would silently desync
        # the install from the container copy.
        body = extract_embedded(INSTALL_TEXT, _FIREWALL_HEREDOC_START, _FIREWALL_HEREDOC_END)
        repo_script = REPO_ROOT / "src" / "outo_models" / "assets" / "scripts" / "firewall-open.sh"
        repo_text = repo_script.read_text(encoding="utf-8")
        assert body == repo_text, (
            "embedded firewall-open.sh heredoc drifted from "
            "container/scripts/firewall-open.sh — re-sync by copying the "
            "repo file into the heredoc (and update this test only if the "
            "wording of the drift message changed)."
        )

    def test_embedded_low_ports_script_matches_repo_byte_for_byte(self) -> None:
        body = extract_embedded(INSTALL_TEXT, _LOW_PORTS_HEREDOC_START, _LOW_PORTS_HEREDOC_END)
        repo_script = (
            REPO_ROOT / "src" / "outo_models" / "assets" / "scripts" / "enable-low-ports.sh"
        )
        assert body == repo_script.read_text(encoding="utf-8")

    def test_embedded_firewall_script_is_bash_clean(self) -> None:
        # A syntax error in either copy must fail tests, not installs.
        body = extract_embedded(INSTALL_TEXT, _FIREWALL_HEREDOC_START, _FIREWALL_HEREDOC_END)
        extracted = REPO_ROOT / "tmp_embedded_firewall_check.sh"
        try:
            extracted.write_text(body, encoding="utf-8")
            result = subprocess.run(  # noqa: S603
                ["bash", "-n", str(extracted)],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode == 0, result.stderr
        finally:
            extracted.unlink(missing_ok=True)

    def test_embedded_firewall_self_elevates(self) -> None:
        # `sudo -n` would deadlock the wizard; pin the interactive form.
        body = extract_embedded(INSTALL_TEXT, _FIREWALL_HEREDOC_START, _FIREWALL_HEREDOC_END)
        assert 'exec sudo bash "$0" "$@"' in body
        assert "command -v sudo" in body


class TestInstallerAppliesLowPortSysctl:
    """The installer must apply net.ipv4.ip_unprivileged_port_start itself.

    Field failure: rootless Podman's host-side `rootlessport` helper cannot
    bind host port 80 while the threshold is still the kernel default 1024
    ("rootlessport cannot expose privileged port 80"). The in-container CLI
    cannot fix this — `hostsys.py` deliberately raises `low_ports_host_required`
    inside a container — and the installer already runs as root on the host,
    so it is the one sanctioned place to do it (AGENTS.md §2.3).

    Contract (see also the tests below):
      * gated on the untouched kernel default 1024 — a deliberate operator
        value must never be clobbered;
      * `if ...; then ... else ...` guarded so `set -euo pipefail` cannot kill
        the install;
      * silent when procfs is unavailable (non-Linux host);
      * idempotent — the helper itself no-ops when the threshold is low enough.
    """

    # The literal invocation, pinned by contract (not a variable, not 0, not 443).
    APPLY = 'if bash "${low_ports_dest}" 80; then'
    CHMOD = 'chmod 0755 "${low_ports_dest}"'

    def _apply_index(self) -> int:
        idx = INSTALL_TEXT.index(self.APPLY)
        return idx

    def test_applies_after_installing_the_helper(self) -> None:
        # The helper must exist and be executable before it is run.
        assert INSTALL_TEXT.index(self.CHMOD) < self._apply_index()

    def test_apply_is_set_e_safe_and_warns_without_failing(self) -> None:
        # `set -euo pipefail` is on: a bare `bash "${low_ports_dest}" 80` would
        # abort the whole install on failure. The if/else form is the guard.
        assert self.APPLY in INSTALL_TEXT
        apply_block = INSTALL_TEXT[self._apply_index() : self._apply_index() + 600]
        assert "else" in apply_block.split("fi", 1)[0]
        assert "[warn]" in apply_block
        # The warn branch points at the same command so the operator can retry.
        assert "low_ports_dest" in apply_block

    def test_gated_on_the_untouched_kernel_default(self) -> None:
        # Only 1024 (the kernel default) triggers an automatic apply; any other
        # operator-chosen value must be left alone.
        gate_block = INSTALL_TEXT[
            INSTALL_TEXT.index("ip_unprivileged_port_start") : self._apply_index()
        ]
        assert '"${low_ports_threshold}" == "1024"' in gate_block
        # The left-as-is branch exists for deliberate values.
        assert "[note]" in gate_block

    def test_threshold_read_is_guarded_and_non_empty_gated(self) -> None:
        # No procfs (non-Linux host) → the read must not fail the install, and
        # the whole block must stay conditional on a non-empty threshold.
        assert (
            "cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || true" in INSTALL_TEXT
        )
        assert '-z "${low_ports_threshold}"' in INSTALL_TEXT

    def test_argument_is_the_literal_product_port(self) -> None:
        invocations = re.findall(r'bash "\$\{low_ports_dest\}" ([^;\s]+)', INSTALL_TEXT)
        assert invocations == ["80"], f"unexpected low-ports invocations: {invocations}"


def _wrapper_text(default_tag: str = "stable") -> str:
    """Render the wrapper body the installer would write.

    Mirrors the outer heredoc: install-time `${image}` is substituted, and
    backslash-escaped `\\${...}` becomes runtime `${...}`.
    """
    body = INSTALL_TEXT.split('cat > "${dest}" <<EOF', 1)[1].split("\nEOF", 1)[0]
    default_ref = f"ghcr.io/llaa33219/outo-models:{default_tag}"
    # Install-time substitution hits only UNESCAPED `${image}` — `\${image}`
    # is runtime text in the generated wrapper (real heredocs never expand
    # escaped refs). Then undo the runtime escapes (`\$` → `$`).
    body = re.sub(r"(?<!\\)\$\{image\}", default_ref, body)
    return body.replace("\\$", "$").replace('\\"', '"')


def _render_wrapper(tmp_path: Path, default_tag: str = "stable") -> Path:
    wrapper = tmp_path / "outo-models"
    wrapper.write_text("#!/usr/bin/env bash\n" + _wrapper_text(default_tag), encoding="utf-8")
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

    def test_moving_tag_triggers_refresh_pull(self, tmp_path: Path) -> None:
        # A stale local :dev image must be refreshed from the registry —
        # the field failure where setup ran an old CLI.
        wrapper = _render_wrapper(tmp_path, default_tag="dev")
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper, bin_dir, tmp_path, extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")}
        )
        assert result.returncode == 0, result.stderr
        log = (tmp_path / "podman.log").read_text(encoding="utf-8")
        assert any(line.startswith("pull --quiet") for line in log.splitlines())

    def test_pinned_tag_skips_refresh_pull(self, tmp_path: Path) -> None:
        # Pinned tags are immutable by convention — no per-invocation pull.
        wrapper = _render_wrapper(tmp_path, default_tag="0.2.0-dev")
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper, bin_dir, tmp_path, extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")}
        )
        assert result.returncode == 0, result.stderr
        log = (tmp_path / "podman.log").read_text(encoding="utf-8")
        assert not any(line.startswith("pull") for line in log.splitlines())

    def test_offline_refresh_falls_back_to_local_image(self, tmp_path: Path) -> None:
        # Moving tag + registry unreachable + image present locally → run anyway.
        wrapper = _render_wrapper(tmp_path, default_tag="dev")
        bin_dir = _fake_podman(tmp_path, pull_ok=False)
        result = _run_wrapper(
            wrapper, bin_dir, tmp_path, extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")}
        )
        assert result.returncode == 0, result.stderr
        assert ":dev status" in result.stdout


class TestWrapperForwardsHostSocketPath:
    def test_forwards_host_socket_mount_and_env_when_socket_exists(self, tmp_path: Path) -> None:
        import socket as socket_mod

        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        sock_path = tmp_path / "host.sock"
        with socket_mod.socket(socket_mod.AF_UNIX) as listener:
            listener.bind(str(sock_path))
            result = _run_wrapper(
                wrapper,
                bin_dir,
                tmp_path,
                extra_env={
                    "OUTO_CONFIG": str(tmp_path / "absent.yaml"),
                    "OUTO_PODMAN_SOCK": str(sock_path),
                },
            )
        assert result.returncode == 0, result.stderr
        assert f"{sock_path}:/run/podman/podman.sock" in result.stdout
        assert f"OUTO_PODMAN_SOCK={sock_path}" in result.stdout
        assert "OUTO_PODMAN_SOCKET=/run/podman/podman.sock" in result.stdout


class TestWrapperForwardsHostCdiDirs:
    """The shim is the only process that can see the host's CDI spec dirs.

    `outo-models start` runs inside a container, where the probe would only
    ever match the container's own mount points — passing those to the host
    podman as `-v` sources fails. So the shim (running on the host) probes
    once and forwards the answer in OUTO_CDI_DIRS.
    """

    def _render_with_probed_dirs(self, tmp_path: Path, candidates: str) -> Path:
        """Wrapper whose probe list points at controlled directories.

        The candidate list is the contract under test (pinned verbatim in
        `test_wrapper_probes_host_cdi_dirs_and_forwards_them`), so swapping it
        for a tmp pair exercises the real probe — the `-d` test, the
        colon-joined accumulation and the conditional emit — against a
        filesystem the test owns, instead of whatever this host happens to
        have installed.
        """
        wrapper = _render_wrapper(tmp_path)
        body = _wrapper_text().replace(
            "for dir in /etc/cdi /var/run/cdi", f"for dir in {candidates}"
        )
        wrapper.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
        return wrapper

    def test_env_override_is_forwarded_verbatim(self, tmp_path: Path) -> None:
        # A colon-joined value the operator set (e.g. a non-default spec
        # location) must survive untouched — including entries that do not
        # exist here, since only the host podman resolves them.
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={
                "OUTO_CONFIG": str(tmp_path / "absent.yaml"),
                "OUTO_CDI_DIRS": f"{tmp_path}/specs:/opt/cdi",
            },
        )
        assert result.returncode == 0, result.stderr
        assert f"OUTO_CDI_DIRS={tmp_path}/specs:/opt/cdi" in result.stdout

    def test_probe_emits_only_the_host_directories_that_exist(self, tmp_path: Path) -> None:
        # Given: one of the two probed host directories exists.
        present = tmp_path / "etc-cdi"
        present.mkdir()
        absent = tmp_path / "run-cdi"
        wrapper = self._render_with_probed_dirs(tmp_path, f"{present} {absent}")
        bin_dir = _fake_podman(tmp_path)
        # When
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")},
        )
        # Then: the existing directory only — podman would otherwise create the
        # missing one on the host as an empty root-owned directory.
        assert result.returncode == 0, result.stderr
        assert f"OUTO_CDI_DIRS={present}" in result.stdout
        assert str(absent) not in result.stdout

    def test_probe_emits_both_existing_directories_in_probe_order(self, tmp_path: Path) -> None:
        first = tmp_path / "etc-cdi"
        second = tmp_path / "run-cdi"
        first.mkdir()
        second.mkdir()
        wrapper = self._render_with_probed_dirs(tmp_path, f"{first} {second}")
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")},
        )
        assert result.returncode == 0, result.stderr
        assert f"OUTO_CDI_DIRS={first}:{second}" in result.stdout

    def test_probe_stays_silent_on_a_host_without_cdi_specs(self, tmp_path: Path) -> None:
        # Given / When: neither probed directory exists (a GPU-less host).
        wrapper = self._render_with_probed_dirs(tmp_path, f"{tmp_path}/a {tmp_path}/b")
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")},
        )
        # Then: no empty env, and the wrapper still runs the command.
        assert result.returncode == 0, result.stderr
        assert "OUTO_CDI_DIRS" not in result.stdout
        assert ":stable status" in result.stdout

    def test_env_override_wins_over_the_host_probe(self, tmp_path: Path) -> None:
        # The operator's value must not be extended by the probe — an override
        # equal to the probed set would pass either implementation.
        probed = tmp_path / "probed-cdi"
        probed.mkdir()
        wrapper = self._render_with_probed_dirs(tmp_path, str(probed))
        bin_dir = _fake_podman(tmp_path)
        override = "/opt/cdi-only"
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={
                "OUTO_CONFIG": str(tmp_path / "absent.yaml"),
                "OUTO_CDI_DIRS": override,
            },
        )
        assert result.returncode == 0, result.stderr
        assert f"OUTO_CDI_DIRS={override}" in result.stdout
        assert str(probed) not in result.stdout


class TestWrapperDestructiveEnvPassThrough:
    """The reset gate reads OUTO_DESTRUCTIVE INSIDE the container — the shim
    must forward it from the host environment (field failure: the gate saw
    the var as unset even when exported on the host)."""

    def test_destructive_env_forwarded_when_set(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={
                "OUTO_CONFIG": str(tmp_path / "absent.yaml"),
                "OUTO_DESTRUCTIVE": "1",
            },
        )
        assert result.returncode == 0, result.stderr
        assert "-e OUTO_DESTRUCTIVE" in result.stdout

    def test_destructive_env_absent_when_unset(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        env = {"OUTO_CONFIG": str(tmp_path / "absent.yaml")}
        result = _run_wrapper(wrapper, bin_dir, tmp_path, extra_env=env)
        assert result.returncode == 0, result.stderr
        assert "OUTO_DESTRUCTIVE" not in result.stdout


class TestWrapperResetDestroyUnmountsVolume:
    """reset --destroy must not hold the volume it deletes (self-kill class)."""

    def test_reset_destroy_skips_volume_mount(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")},
            args=["reset", "--destroy"],
        )
        assert result.returncode == 0, result.stderr
        assert "outo-models-data:/var/lib/outo-models" not in result.stdout

    def test_reset_dry_run_keeps_volume_mount(self, tmp_path: Path) -> None:
        wrapper = _render_wrapper(tmp_path)
        bin_dir = _fake_podman(tmp_path)
        result = _run_wrapper(
            wrapper,
            bin_dir,
            tmp_path,
            extra_env={"OUTO_CONFIG": str(tmp_path / "absent.yaml")},
            args=["reset"],
        )
        assert result.returncode == 0, result.stderr
        assert "outo-models-data:/var/lib/outo-models" in result.stdout
