"""Helpers for building the public clone URL of a repository."""

from __future__ import annotations

from outo_models.config import get_settings


def clone_url(owner: str, name: str) -> str:
    """Return the HTTPS clone URL a client would `git clone` from.

    The scheme (http vs https) is decided by `Settings.base_url`, which
    returns http for loopback domains and https everywhere else.
    """
    base = get_settings().base_url
    return f"{base}/{owner}/{name}.git"
