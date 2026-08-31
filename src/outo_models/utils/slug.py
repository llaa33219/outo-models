"""Slug normalization and validation.

A slug is the canonical URL-safe identifier for a user, repo, or space.
The policy matches Hugging Face's well-known constraints and is enforced
at every public input boundary.
"""

from __future__ import annotations

import re

from outo_models.exceptions import ValidationFailedError

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
_MAX_SLUG_LENGTH = 63


def normalize_slug(value: str) -> str:
    """Lowercase the input and strip surrounding whitespace.

    Does not collapse or replace internal whitespace — callers pick the
    separator that fits their domain (e.g. `-` for repos, `.` for versions).
    """
    return value.strip().lower()


def validate_slug(value: str) -> str:
    """Validate that `value` matches the slug policy; return it unchanged.

    Policy (mirrors the regex the API contract documents):
        - 1 to 63 characters.
        - First character: lowercase ASCII letter or digit.
        - Remaining characters: lowercase ASCII letter, digit, dot, underscore,
          or hyphen.
        - Must NOT start or end with `.` or `-`.

    Raises:
        ValidationFailedError: if `value` violates the policy.
    """
    if not value:
        raise ValidationFailedError("slug must not be empty")
    if len(value) > _MAX_SLUG_LENGTH:
        raise ValidationFailedError(f"slug must be at most {_MAX_SLUG_LENGTH} characters")
    if not _SLUG_PATTERN.match(value):
        raise ValidationFailedError("slug must start with [a-z0-9] and contain only [a-z0-9._-]")
    if value[0] in (".", "-") or value[-1] in (".", "-"):
        # The regex already forbids leading '.' / '-'; this guards the trailing
        # case explicitly so the rule reads the same way as the public spec.
        raise ValidationFailedError("slug must not start or end with '.' or '-'")
    return value
