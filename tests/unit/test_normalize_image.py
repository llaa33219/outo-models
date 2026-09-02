"""Unit tests for the `normalize_image_ref` helper.

The setup wizard asks the operator for an image track (`stable` / `dev` /
`custom`) and persists the result into `config.yaml` as a full image
reference (e.g. `ghcr.io/llaa33219/outo-models:stable`). `update.py` and
`start.py` consume that exact string. The helper below is the single
funnel that converts an operator-typed value into a normalized reference.

This file lives in `tests/unit` because the helper is a pure function —
no I/O, no DB, no environment variable lookups — so the test is fast
and the function can be reused in `update.py` without spinning up a
wizard. Validation failures use `ValidationFailedError` so the wizard
can re-prompt via the typed-error funnel (`OutoError.code` is the
stable machine contract; the message is the human string).
"""

from __future__ import annotations

import pytest

from outo_models.cli.setup._collect import _DEFAULT_IMAGE_REGISTRY, normalize_image_ref
from outo_models.exceptions import ValidationFailedError


class TestNormalizeBareTag:
    """Bare tags get the default registry prepended."""

    def test_stable_track(self) -> None:
        assert normalize_image_ref("stable") == f"{_DEFAULT_IMAGE_REGISTRY}:stable"

    def test_dev_track(self) -> None:
        assert normalize_image_ref("dev") == f"{_DEFAULT_IMAGE_REGISTRY}:dev"

    def test_pinned_stable_version(self) -> None:
        assert normalize_image_ref("0.2.0-stable") == f"{_DEFAULT_IMAGE_REGISTRY}:0.2.0-stable"

    def test_pinned_dev_version(self) -> None:
        assert normalize_image_ref("0.2.0-dev") == f"{_DEFAULT_IMAGE_REGISTRY}:0.2.0-dev"

    def test_per_arch_tag(self) -> None:
        assert (
            normalize_image_ref("0.2.0-stable-arm64")
            == f"{_DEFAULT_IMAGE_REGISTRY}:0.2.0-stable-arm64"
        )

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert normalize_image_ref("  stable  ") == f"{_DEFAULT_IMAGE_REGISTRY}:stable"


class TestNormalizeFullReference:
    """A reference containing `/` is treated as already complete."""

    def test_localhost_reference_passes_through(self) -> None:
        assert normalize_image_ref("localhost/outo-models:stable") == "localhost/outo-models:stable"

    def test_fork_ghcr_path_passes_through(self) -> None:
        assert (
            normalize_image_ref("ghcr.io/some-fork/outo-models:0.2.0-stable")
            == "ghcr.io/some-fork/outo-models:0.2.0-stable"
        )

    def test_default_ghcr_path_passes_through_when_explicit(self) -> None:
        # Operators who happen to type the full default reference must not
        # see a second prepending.
        full = f"{_DEFAULT_IMAGE_REGISTRY}:stable"
        assert normalize_image_ref(full) == full


class TestNormalizeRejects:
    """Empty / whitespace-only / values containing spaces raise."""

    @pytest.mark.parametrize("value", ["", " ", "   ", "\t\n"])
    def test_empty_or_whitespace_raises(self, value: str) -> None:
        with pytest.raises(ValidationFailedError):
            normalize_image_ref(value)

    @pytest.mark.parametrize(
        "value",
        [
            "stable image",
            " 0.2.0 stable ",
            "foo bar baz",
        ],
    )
    def test_contains_space_raises(self, value: str) -> None:
        with pytest.raises(ValidationFailedError):
            normalize_image_ref(value)
