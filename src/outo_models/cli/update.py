"""`outo-models update` — host-side script wrapper.

Delegates to `container/scripts/update.sh` (resolved via
`container_script()`) which performs:

    1. `podman pull <image-tag>`
    2. `podman run --rm ... outo-models migrate` (throwaway container)
    3. `podman restart outo-models` (if the container is running)

The CLI's only job is to locate the script, build its argv, stream its
output, and translate its exit code into a CLI exit. The wizard and the
update path both speak the same script, so the safety properties of the
update flow live in `update.sh`, not in this file.

When `--image` is omitted, the image reference is read from the same
`/etc/outo-models/config.yaml` the setup wizard wrote. The fallback
(when the file or its `image` key is missing) is the official stable
image — `update` is only meaningful after `setup` has run, but a
missing config should not brick a one-off `outo-models update` call.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
import yaml

from outo_models.cli import (
    container_script,
    render_error,
    stream_subprocess,
    typer_exit,
)
from outo_models.cli.setup._collect import (
    _DEFAULT_IMAGE_REGISTRY,
    normalize_image_ref,
)
from outo_models.exceptions import OutoError

_FALLBACK_IMAGE = f"{_DEFAULT_IMAGE_REGISTRY}:stable"
_DEFAULT_CONFIG = Path("/etc/outo-models/config.yaml")


def _config_path() -> Path:
    """Return the YAML config path, honoring `OUTO_CONFIG` (same as `start`)."""
    override = os.environ.get("OUTO_CONFIG")
    if override:
        return Path(override)
    return _DEFAULT_CONFIG


def _image_from_config() -> str:
    """Read the `image` key from the wizard's YAML config.

    Returns `_FALLBACK_IMAGE` when the file is absent, unreadable, or
    does not contain an `image` key. A bad / malformed YAML file is
    treated the same as a missing file — `update` is a host-side
    convenience, and surfacing a stack trace here would defeat the
    wizard's "always recoverable" promise.
    """
    path = _config_path()
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh)
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return _FALLBACK_IMAGE
    if not isinstance(payload, dict):
        return _FALLBACK_IMAGE
    image = payload.get("image")
    if not isinstance(image, str) or not image:
        return _FALLBACK_IMAGE
    return image


def update(
    image: str | None = typer.Option(
        None,
        "--image",
        help=(
            "Image reference to update to. Defaults to the `image` key "
            "in /etc/outo-models/config.yaml (the same value `start` "
            "uses), or `ghcr.io/llaa33219/outo-models:stable` when the "
            "config is missing."
        ),
    ),
) -> None:
    """`outo-models update` — pull the new image, run DB migrations, restart."""
    image_ref = normalize_image_ref(image) if image is not None else _image_from_config()

    script = container_script("update.sh")
    argv = ["bash", script, image_ref]
    rc = stream_subprocess(argv)
    if rc != 0:
        render_error(
            OutoError(
                f"update script failed (exit={rc})",
                code="update_failed",
            )
        )
        raise typer_exit(1)


__all__ = ["update"]
