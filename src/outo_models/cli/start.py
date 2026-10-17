"""`outo-models start` — `podman run` the outo-models container.

The wizard writes `/etc/outo-models/config.yaml` on first run, and `start`
reads it back so it knows which image tag, volume name, and ports to use.
If the config file is missing the command refuses to run — the operator
must `setup` first. The container runs unprivileged (see AGENTS.md §2.3):
firewall / DNS / TLS work is already done by the setup wizard, and this
command does NOT touch any host-privileged tooling.

When `podman` is not on `PATH` (the development machine), the command
prints a clear message telling the operator this command runs on the
server host. This is *not* an error per se — the development machine
has no server to manage — but it exits non-zero so a CI script wrapping
the CLI notices.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import typer
import yaml

from outo_models.cli import (
    podman_available,
    podman_base,
    print_status,
    render_error,
    run_capture,
    typer_exit,
)
from outo_models.config import Settings, get_settings
from outo_models.exceptions import ConfigError, OutoError
from outo_models.firewall.open_ports import in_container
from outo_models.hostsys import HOST_LOW_PORTS_COMMAND, low_ports_blocked

# The default location of the YAML the wizard writes. `OUTO_CONFIG`
# overrides it for testing and for operators who vendor the file under
# `/etc/outo-models/config.yaml` (the production default).
_DEFAULT_CONFIG = Path("/etc/outo-models/config.yaml")

# What fields `start` actually consumes — the wizard writes a few more
# keys it owns (`admin_username`, `dns_provider`, ...) that `start` does
# not need. Being explicit about which keys we read prevents the command
# from accidentally adopting a config field meant for a different surface.
_KEY_IMAGE = "image"
_KEY_VOLUME = "volume"
_KEY_PORTS = "ports"
_REQUIRED_KEYS: tuple[str, ...] = (_KEY_IMAGE, _KEY_VOLUME, _KEY_PORTS)

# Where the host's Podman API socket is mounted INSIDE the server container.
# Same default as config.py's `podman_socket`, stated explicitly here so the
# mount and the env var can never drift apart.
_CONTAINER_PODMAN_SOCKET = "/run/podman/podman.sock"


def _probe_host_podman_sockets() -> tuple[str, ...]:
    """Return host socket candidates to probe, rootless candidate first.

    The server container runs as uid 1000, so a rootful `root:root 0660`
    socket is not connectable; the rootless user socket is the supported path.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return (f"{runtime_dir}/podman/podman.sock", _CONTAINER_PODMAN_SOCKET)


def _host_podman_socket() -> str | None:
    """Return the host Podman socket path to mount, or None when there is none.

    `OUTO_PODMAN_SOCK` (set by the install-cli.sh shim) is the host path and is
    used verbatim even when it is not visible from this process: the HOST
    podman resolves the `-v` source, not the CLI container.

    Without it, only a host shell may probe — inside the CLI container every
    path we can see is the container's own mount point, and passing that as a
    host source would make `podman run` fail on a nonexistent host path.
    """
    override = os.environ.get("OUTO_PODMAN_SOCK")
    if override:
        return override
    if in_container():
        return None
    for candidate in _probe_host_podman_sockets():
        try:
            is_socket = Path(candidate).is_socket()
        except OSError:
            continue
        if is_socket:
            return candidate
    return None


# Where the host keeps its CDI spec files. podman < 6.0 has no CDI concept in
# its REST API, so the server resolves the specs itself on the fallback GPU
# path — which means it has to be able to read them.
_CDI_SPEC_DIRS = ("/etc/cdi", "/var/run/cdi")


def _host_cdi_dirs() -> tuple[str, ...]:
    """Return the CDI spec directories to mount read-only, existing ones only.

    An absent directory is omitted silently: `podman run -v` would otherwise
    create it on the host as an empty root-owned directory. Like the socket
    probe, nothing is probed from inside the CLI container — there every
    visible path is the container's own mount point, and passing that as a
    `-v` source makes the host podman fail on a nonexistent host path.
    """
    if in_container():
        return ()
    found: list[str] = []
    for candidate in _CDI_SPEC_DIRS:
        try:
            is_dir = Path(candidate).is_dir()
        except OSError:
            continue
        if is_dir:
            found.append(candidate)
    return tuple(found)


def _config_path() -> Path:
    """Return the YAML config path, honoring `OUTO_CONFIG`."""
    override = os.environ.get("OUTO_CONFIG")
    if override:
        return Path(override)
    return _DEFAULT_CONFIG


def _load_config() -> dict[str, object]:
    """Read and validate the YAML config the wizard produced.

    Raises:
        ConfigError: when the file is missing (operator needs to run
            `setup` first) or when it is missing required keys.
    """
    path = _config_path()
    if not path.exists():
        raise ConfigError(f"config file not found ({path}). Run 'outo-models setup' first.")
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh) or {}
    if not isinstance(payload, dict):
        raise ConfigError(f"config file has an invalid format ({path}): top-level is not a mapping")
    for key in _REQUIRED_KEYS:
        if key not in payload:
            raise ConfigError(f"config file is missing the '{key}' key ({path})")
    return payload


_CONTAINER_NAME = "outo-models"


def _inspect_state() -> str:
    """Container state via podman inspect; "missing" when inspect fails."""
    base = podman_base()
    if not base:
        return "missing"
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*base, "inspect", "--format", "{{.State.Status}}", _CONTAINER_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "missing"
    return result.stdout.strip()


def _container_image_id() -> str | None:
    """Image ID (digest) the existing container was created from."""
    base = podman_base()
    if not base:
        return None
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*base, "inspect", "--format", "{{.Image}}", _CONTAINER_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _local_image_id(image: str) -> str | None:
    """Current local image ID for a reference (moving tags move!)."""
    base = podman_base()
    if not base:
        return None
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*base, "image", "inspect", "--format", "{{.Id}}", image],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _container_image_matches(image: str) -> bool:
    """True iff the existing container runs exactly the configured image.

    Compares DIGESTS, not names: `:dev` / `:stable` are moving tags, so a
    name match means nothing after a pull (field failure: a pre-Caddy
    container kept running because both said `:dev`). Unknowable → False
    (replace is the safe direction).
    """
    container_id = _container_image_id()
    local_id = _local_image_id(image)
    return container_id is not None and local_id is not None and container_id == local_id


def _remove_container() -> None:
    """Force-remove the existing container (any state); idempotent."""
    base = podman_base()
    if not base:
        return
    subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*base, "rm", "-f", _CONTAINER_NAME],
        check=False,
        capture_output=True,
    )


def _podman_is_rootless() -> bool | None:
    """Whether podman runs rootless here, or None when it cannot be told.

    Asks the engine itself (`podman info`) instead of guessing from the
    environment: only the engine knows which mode it booted in. Fails OPEN —
    every uncertain outcome (missing binary, non-zero exit, unexpected output,
    timeout) returns None so a detection hiccup never blocks a start that
    would otherwise work. The caller treats None as "assume rootful".
    """
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [*podman_base(), "info", "--format", "{{.Host.Security.Rootless}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    answer = (result.stdout or "").strip()
    if answer == "true":
        return True
    if answer == "false":
        return False
    return None


def _preflight_low_ports(ports: list[int]) -> None:
    """Refuse to start when rootless podman cannot publish the low ports.

    On a rootful engine the kernel lets `podman run` bind :80 itself, so the
    threshold never matters; on a rootless one the HOST-side `rootlessport`
    helper performs the bind and is subject to
    `net.ipv4.ip_unprivileged_port_start`. Rather than let the run die with a
    cryptic exit 126, fail here — before the existing container is touched —
    with the exact host command that fixes it.

    Raises:
        OutoError(low_ports_blocked): rootless podman + a threshold above the
            lowest configured port. The message carries
            `HOST_LOW_PORTS_COMMAND` rendered for that port.
    """
    if not ports:
        return
    min_port = min(ports)
    if not low_ports_blocked(min_port):
        return
    if _podman_is_rootless() is not True:
        return
    command = HOST_LOW_PORTS_COMMAND.replace("<min-port>", str(min_port))
    raise OutoError(
        f"podman is rootless and the host only allows unprivileged binds from "
        f"net.ipv4.ip_unprivileged_port_start, so port {min_port} cannot be "
        f"published: the host-side rootlessport helper has to bind it. Enable "
        f"low ports on the host (the script asks for sudo itself):\n\n"
        f"    {command}\n",
        code="low_ports_blocked",
    )


def _probe(url: str) -> bool:
    """One HTTP probe against the health endpoint; True only on a 200."""
    import httpx

    try:
        response = httpx.get(url, timeout=3.0, verify=False)  # noqa: S501 — startup probe; cert validity is ACME's concern, not this check's
        return response.status_code == 200
    except httpx.HTTPError:
        return False


def _health_url(settings: Settings, ports: list[str]) -> str:
    """Where the verification polls: loopback HTTP internally, domain HTTPS otherwise."""
    if settings.is_internal:
        return f"http://127.0.0.1:{ports[0]}/healthz"
    tls_port = "443" if "443" in ports else ports[-1]
    return f"https://{settings.domain}:{tls_port}/healthz"


def _collect_logs() -> str:
    """Best-effort `podman logs` tail, captured for display + diagnosis."""
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [*podman_base(), "logs", "--tail", "50", _CONTAINER_NAME],
        check=False,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "") + (result.stderr or "")


# Ordered (pattern, advice) rules. The first match wins, so keep the most
# specific signatures at the top. Every advice names the exact command an
# operator can paste — no "see the docs" without a concrete first step.
_DIAGNOSIS_RULES: tuple[tuple[str, str], ...] = (
    (
        "rootlessport cannot expose privileged port",
        """\
Podman could not publish the low port: the HOST-side rootlessport helper has
to bind it, and the kernel only lets unprivileged binds start at
net.ipv4.ip_unprivileged_port_start (1024). Note that `--cap-add
NET_BIND_SERVICE` applies INSIDE the container — it cannot help the host-side
rootlessport helper, which is the one failing here. Fix it once on the host:

    /usr/local/share/outo-models/enable-low-ports.sh 80

(it asks for sudo and applies a persistent sysctl). Then: outo-models start""",
    ),
    (
        "listen tcp :",
        """\
The kernel refused to let Caddy bind a low port (rootless containers may
only bind ports >= 1024 by default). Fix it once on the host:

    /usr/local/share/outo-models/enable-low-ports.sh 80

(it asks for sudo and applies a persistent sysctl). Then: outo-models start""",
    ),
    (
        "address already in use",
        """\
Another program already occupies the port. Find it with:

    sudo ss -lntp | grep -E ':(80|443)\\\\s'

Stop that program (or change the ports via `outo-models setup`), then:
outo-models start""",
    ),
    (
        "No such command",
        """\
The CLI and the image disagree about command names — the image is stale.
Update everything and retry:

    outo-models update
    outo-models start""",
    ),
    (
        "unable to open database file",
        """\
The server cannot write its database — usually leftover files owned by a
different uid after a sudo/rootless mix-up. If there is no data to keep,
the clean path is a full reset:

    OUTO_DESTRUCTIVE=1 outo-models reset --destroy
    outo-models setup
    outo-models start""",
    ),
    (
        "acme",
        """\
Certificate issuance (ACME) did not finish in time. This is normal for a
few seconds on the very first start with a public domain — just retry:

    outo-models start --verify-timeout 120

If it keeps failing, check that the DNS record for your domain points at
this machine and ports 80/443 are reachable from the internet
(docs/troubleshooting.md, ACME section).""",
    ),
)


def _print_diagnosis(logs: str, url: str = "") -> None:
    """Turn a failure output into an actionable, plain-language next step.

    `url` is the health-probe URL when one exists; failures raised before the
    container ever serves (a rejected `podman run`) pass "" and the generic
    fallback drops the probe clause rather than naming a URL nobody polled.
    """
    lowered = logs.lower()
    for signature, advice in _DIAGNOSIS_RULES:
        if signature.lower() in lowered:
            print_status("\n[what to do] " + advice)
            return
    fallback = (
        "\n[what to do] no known failure pattern matched. The log above is the "
        "source of truth; docs/troubleshooting.md lists the common cases. "
        "If nothing applies, re-run with a longer window: outo-models start "
        "--verify-timeout 180"
    )
    if url:
        fallback += f" (probe was {url})"
    print_status(fallback)


def _verify_started(settings: Settings, ports: list[str], timeout: float) -> None:
    """Block until the stack answers /healthz, or fail with diagnostics.

    Catches the field failure where `podman run -d` returned 0 but the
    container died instantly (Caddy bind errors, bad config, …)."""
    url = _health_url(settings, ports)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _inspect_state()
        if state in ("exited", "configured", "created", "dead"):
            break  # dead on arrival — no point polling the URL
        if state == "running" and _probe(url):
            print_status(f"[done] server is up: {settings.base_url}")
            print_status(f"(health probe: {url})")
            return
        time.sleep(1.0)
    print_status("[error] server did not become healthy in time — last 50 log lines:")
    logs = _collect_logs()
    if logs.strip():
        print_status(logs)
    _print_diagnosis(logs, url)
    raise OutoError(
        f"server failed to become healthy within {timeout:.0f}s (probe: {url}). "
        "See the log tail above; common causes are the low-port sysctl "
        "(docs/troubleshooting.md §2) and TLS/ACME issuance delays.",
        code="start_verify_failed",
    )


def start(
    no_verify: bool = typer.Option(
        False, "--no-verify", help="Do not wait for the server to become healthy."
    ),
    verify_timeout: float = typer.Option(
        60.0, "--verify-timeout", help="Seconds to wait for the health check."
    ),
) -> None:
    """`outo-models start` — start the container."""
    if not podman_available():
        render_error(
            ConfigError(
                "this command must be run on the server host (podman not installed). "
                "Re-run it on the host of the deployed container."
            )
        )
        raise typer_exit(1)

    try:
        cfg = _load_config()
    except ConfigError as exc:
        render_error(exc)
        raise typer_exit(1) from exc

    image = str(cfg[_KEY_IMAGE])
    volume = str(cfg[_KEY_VOLUME])
    ports_raw = cfg[_KEY_PORTS]
    if not isinstance(ports_raw, list):
        render_error(
            ConfigError(f"config file 'ports' must be a list (got {type(ports_raw).__name__})")
        )
        raise typer_exit(1)
    try:
        port_numbers = [int(p) for p in ports_raw]
    except (TypeError, ValueError) as exc:
        render_error(
            ConfigError(
                f"config file 'ports' has an invalid entry ({ports_raw!r}): ports must be integers"
            )
        )
        raise typer_exit(1) from exc
    ports = [str(p) for p in port_numbers]

    # An existing container with this name blocks `podman run` (exit 125 in
    # the field). Running with the same image → nothing to do but verify.
    # Anything else (stopped, or a stale image after `update`) → replace it:
    # `podman restart` would boot the OLD image, which is never what an
    # operator means by `start` right after an update.
    settings = get_settings()
    existing_state = _inspect_state()
    if existing_state == "running" and _container_image_matches(image):
        print_status(f"[status] already running: {_CONTAINER_NAME} ({image})")
        if not no_verify:
            try:
                _verify_started(settings, ports, verify_timeout)
            except OutoError as exc:
                render_error(exc)
                raise typer_exit(1) from exc
        return
    # Ordering invariant: preflight precedes every container manipulation, so
    # an abort never destroys an existing (stopped) container the operator can
    # still start by hand after fixing the host sysctl.
    try:
        _preflight_low_ports(port_numbers)
    except OutoError as exc:
        render_error(exc)
        raise typer_exit(1) from exc
    if existing_state != "missing":
        print_status(f"[info] replacing existing container (state={existing_state})")
        _remove_container()

    # Mirror the settings env so the in-container process binds to the
    # same data_dir / secret_key the wizard wrote.
    env_args: list[str] = []
    if settings.data_dir:
        env_args.extend(["-e", f"OUTO_DATA_DIR={settings.data_dir}"])
    if settings.secret_key:
        env_args.extend(["-e", f"OUTO_SECRET_KEY={settings.secret_key}"])
    if settings.domain:
        env_args.extend(["-e", f"OUTO_DOMAIN={settings.domain}"])
    if settings.require_approval is not None:
        env_args.extend(
            ["-e", f"OUTO_REQUIRE_APPROVAL={'true' if settings.require_approval else 'false'}"]
        )
    if settings.db_url:
        env_args.extend(["-e", f"OUTO_DB_URL={settings.db_url}"])
    # Caddy's DNS-01 plugin reads the token from its own process env; the
    # wizard stores it in the config file, and this is the only hop.
    cf_token = cfg.get("cloudflare_api_token")
    if cf_token:
        env_args.extend(["-e", f"CLOUDFLARE_API_TOKEN={cf_token}"])

    # Spaces container SDKs talk to the host Podman API through this socket.
    # Mounting it here is what makes a default install able to run them.
    podman_sock = _host_podman_socket()
    if podman_sock:
        env_args.extend(["-e", f"OUTO_PODMAN_SOCKET={_CONTAINER_PODMAN_SOCKET}"])
    else:
        print_status(
            "[note] no Podman socket found — Spaces container SDKs (docker/gradio/streamlit) "
            "stay unavailable until a Podman socket is mounted."
        )

    argv: list[str] = [
        *podman_base(),
        "run",
        "-d",
        "--name",
        "outo-models",
        # Rootless userns mapping must match the CLI shim (keep-id) so files
        # in the shared volume stay owned by the invoking host user instead
        # of subuids — mixed ownership breaks the DB.
        "--userns=keep-id",
        # The Spaces run proxy forwards to 127.0.0.1:<host_port>, where
        # rootless portmappings bind on the HOST. Loopback is per-netns, so the
        # server must share the host netns — the `-p` mappings this replaces
        # published ports but could never make that loopback reachable.
        "--network=host",
        *env_args,
        "-v",
        f"{volume}:/var/lib/outo-models",
        # The wizard's Caddyfile + config.yaml live here; read-only.
        "-v",
        "/etc/outo-models:/etc/outo-models:ro",
        # Read-only: the server only needs to talk to Podman, never write it.
        *(["-v", f"{podman_sock}:{_CONTAINER_PODMAN_SOCKET}:ro"] if podman_sock else []),
        # Read-only: the server resolves CDI specs itself on podman < 6.0.
        *[arg for directory in _host_cdi_dirs() for arg in ("-v", f"{directory}:{directory}:ro")],
        "--cap-add",
        "NET_BIND_SERVICE",
    ]
    argv.append(image)

    # Capture instead of streaming: on a failed run podman's own message is
    # the diagnosis input (e.g. the rootlessport low-port refusal), and
    # inherited stderr would consume it.
    rc, output = run_capture(argv)
    if rc != 0:
        captured = output.strip()
        if captured:
            print_status(captured)
        print_status(f"[error] container failed to start (exit={rc})")
        _print_diagnosis(output, "")
        raise typer_exit(1)

    if no_verify:
        print_status(f"container started (unverified): {image}")
        return

    try:
        _verify_started(settings, ports, verify_timeout)
    except OutoError as exc:
        render_error(exc)
        raise typer_exit(1) from exc


__all__ = ["start"]
