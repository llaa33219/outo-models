"""The human-byte parser is reachable from the util module (router-safe path).

Routers cannot import `outo_models.cli` (it pulls `rich` + `typer` at module
scope), so the parser lives in `outo_models.utils.humanbytes`. These tests pin
the *import path* and a representative slice of the acceptance table; the
full table is exercised through the CLI re-export in the CLI integration
tests.
"""

from __future__ import annotations

import pytest

from outo_models.exceptions import ValidationFailedError
from outo_models.utils.humanbytes import parse_human_bytes


class TestParseHumanBytes:
    def test_binary_unit_when_suffixed_value(self) -> None:
        assert parse_human_bytes("10GiB") == 10 * 1024**3

    def test_decimal_unit_when_suffixed_value(self) -> None:
        assert parse_human_bytes("500MB") == 500 * 1000**2

    def test_bare_integer_when_no_unit(self) -> None:
        assert parse_human_bytes("1024") == 1024

    def test_rejects_garbage_when_malformed(self) -> None:
        with pytest.raises(ValidationFailedError):
            parse_human_bytes("not-a-size")

    def test_cli_re_export_is_the_same_function(self) -> None:
        from outo_models.cli import parse_human_bytes as cli_parse

        assert cli_parse is parse_human_bytes
