"""Human-readable byte-size parsing, shared by the CLI and the routers.

`outo_models.cli` imports `rich` + `typer` at module scope, so routers cannot
reach a parser that lives there. The parser lives here; `outo_models.cli`
re-exports it so existing imports and the documented CLI surface keep working.
"""

from __future__ import annotations

_BINARY_UNITS: dict[str, int] = {
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
    "PIB": 1024**5,
}

_DECIMAL_UNITS: dict[str, int] = {
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "PB": 1000**5,
}


def parse_human_bytes(text_value: str) -> int:
    """Parse `10GiB` / `500MiB` / `0` / `1024` style strings into bytes.

    Accepts the binary-suffixed units (`KiB`, `MiB`, `GiB`, `TiB`) and
    decimal-suffixed units (`KB`, `MB`, `GB`, `TB`); each is interpreted
    in its natural base (2^10 / 10^3 respectively). Case-insensitive.

    Used by `outo-models admin quota set`. Raises `ValidationFailedError`
    on malformed input — the same error type the API surfaces — so the
    operator gets a uniform message regardless of which surface they used.
    """
    from outo_models.exceptions import ValidationFailedError

    raw = text_value.strip()
    if not raw:
        raise ValidationFailedError("quota value must not be empty")

    upper = raw.upper().replace(" ", "")
    for unit, factor in _BINARY_UNITS.items():
        if upper.endswith(unit):
            number = upper[: -len(unit)]
            return _parse_int(number, factor, raw)
    for unit, factor in _DECIMAL_UNITS.items():
        if upper.endswith(unit):
            number = upper[: -len(unit)]
            return _parse_int(number, factor, raw)

    try:
        return int(raw)
    except ValueError as exc:
        raise ValidationFailedError(
            f"quota value {text_value!r} is not a valid size "
            "(use e.g. 10GiB, 500MiB, or 10737418240)"
        ) from exc


def _parse_int(number_text: str, factor: int, original: str) -> int:
    from outo_models.exceptions import ValidationFailedError

    try:
        return int(float(number_text) * factor)
    except ValueError as exc:
        raise ValidationFailedError(f"quota value {original!r} has a non-numeric prefix") from exc


__all__ = ["parse_human_bytes"]
