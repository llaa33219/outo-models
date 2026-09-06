"""Tests for include / exclude glob filtering."""

from __future__ import annotations

from outo_models_cli.matchers import matches, passes


def test_matches_basename_glob() -> None:
    assert matches("weights/a.safetensors", "*.safetensors")


def test_matches_full_path_glob() -> None:
    assert matches("weights/a.safetensors", "weights/*.safetensors")
    assert not matches("checkpoints/a.safetensors", "weights/*.safetensors")


def test_matches_strips_leading_dot_slash() -> None:
    assert matches("a.txt", "./a.txt")


def test_matches_strips_trailing_slash() -> None:
    assert matches("a.txt", "a.txt/")


def test_matches_replaces_backslashes() -> None:
    """Windows-style globs must still work against forward-slash paths."""
    assert matches("dir/a.txt", "dir\\*.txt")


def test_passes_when_no_patterns() -> None:
    assert passes("anything.txt")


def test_passes_include_matches() -> None:
    assert passes("a.safetensors", include=["*.safetensors"])


def test_passes_include_misses() -> None:
    assert not passes("a.bin", include=["*.safetensors"])


def test_passes_exclude_blocks() -> None:
    assert not passes("a.bin", exclude=["*.bin"])


def test_exclude_wins_over_include() -> None:
    """An explicit exclude beats a coincidental include match."""
    assert not passes(
        "a.safetensors",
        include=["*.safetensors"],
        exclude=["a.safetensors"],
    )


def test_include_with_multiple_globs() -> None:
    assert passes("a.safetensors", include=["*.bin", "*.safetensors"])
    assert not passes("a.txt", include=["*.bin", "*.safetensors"])
