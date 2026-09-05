"""Unit tests for `outo_models.repos.card` (README + front matter + render).

The contract:

    * `read_readme` returns `None` for missing / empty / branchless repos
      and the decoded text otherwise.
    * The README lookup is case-insensitive over the canonical three
      names (`README.md`, `README.MD`, `readme.md`).
    * `parse_card_metadata` parses valid YAML front matter and renders
      the body to HTML with raw HTML escaped (XSS guard).
    * Broken / missing YAML is tolerated — empty front matter and the
      original text are returned.
    * Convenience properties read HF-style keys (task / datasets /
      base_model / license / tags / language) and tolerate single-string
      vs. list shapes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dulwich import porcelain

from outo_models.config import get_settings
from outo_models.repos.card import (
    CardMetadata,
    parse_card_metadata,
    read_card,
    read_readme,
)
from outo_models.repos.storage import repo_fs_path


def _seed_bare_repo_with_readme(
    tmp_data_dir: Path,
    *,
    owner: str,
    name: str,
    readme_name: str,
    readme_text: str,
) -> None:
    """Build a bare repo with a single README file committed to `main`."""
    work = tmp_data_dir / "src"
    work.mkdir()
    (work / readme_name).write_text(readme_text, encoding="utf-8")
    porcelain.init(str(work))
    porcelain.add(str(work), paths=[readme_name])
    porcelain.commit(
        str(work),
        message=b"init",
        author=b"alice <a@example.com>",
        committer=b"alice <a@example.com>",
    )
    bare = repo_fs_path(owner, name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    porcelain.clone(str(work), str(bare), bare=True)
    get_settings.cache_clear()


@pytest.fixture
def reset_settings() -> None:
    """Refresh `get_settings` so the tmpdir-bound data_dir is in scope."""
    get_settings.cache_clear()
    yield  # type: ignore[misc]
    get_settings.cache_clear()


class TestReadReadme:
    async def test_returns_none_for_missing_repo(
        self, tmp_data_dir: Path, reset_settings: None
    ) -> None:
        assert await read_readme("ghost", "nope") is None

    async def test_returns_none_for_empty_repo(
        self, tmp_data_dir: Path, reset_settings: None
    ) -> None:
        work = tmp_data_dir / "src"
        work.mkdir()
        porcelain.init(str(work))
        bare = repo_fs_path("alice", "empty")
        bare.parent.mkdir(parents=True, exist_ok=True)
        porcelain.clone(str(work), str(bare), bare=True)
        assert await read_readme("alice", "empty") is None

    async def test_returns_none_when_no_readme(
        self, tmp_data_dir: Path, reset_settings: None
    ) -> None:
        work = tmp_data_dir / "src"
        work.mkdir()
        (work / "a.txt").write_text("hi")
        porcelain.init(str(work))
        porcelain.add(str(work), paths=["a.txt"])
        porcelain.commit(
            str(work),
            message=b"init",
            author=b"a <a@a>",
            committer=b"a <a@a>",
        )
        bare = repo_fs_path("alice", "no-md")
        bare.parent.mkdir(parents=True, exist_ok=True)
        porcelain.clone(str(work), str(bare), bare=True)
        assert await read_readme("alice", "no-md") is None

    async def test_reads_canonical_readme(self, tmp_data_dir: Path, reset_settings: None) -> None:
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="r1",
            readme_name="README.md",
            readme_text="# Title\n\nbody",
        )
        text = await read_readme("alice", "r1")
        assert text == "# Title\n\nbody"

    @pytest.mark.parametrize(
        "readme_name",
        ["README.md", "README.MD", "readme.md"],
    )
    async def test_readme_case_variants(
        self, tmp_data_dir: Path, reset_settings: None, readme_name: str
    ) -> None:
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="r-case",
            readme_name=readme_name,
            readme_text=f"# via {readme_name}",
        )
        text = await read_readme("alice", "r-case")
        assert text == f"# via {readme_name}"


class TestParseCardMetadata:
    def test_empty_input_returns_empty_metadata(self) -> None:
        card = parse_card_metadata("")
        assert card.front_matter == {}
        assert card.body_html == ""

    def test_parses_valid_front_matter_and_renders_body(self) -> None:
        text = (
            "---\n"
            "task: text-classification\n"
            "datasets:\n"
            "  - glue\n"
            "license: apache-2.0\n"
            "---\n"
            "# Hello\n\nThis is the body.\n"
        )
        card = parse_card_metadata(text)
        assert card.front_matter == {
            "task": "text-classification",
            "datasets": ["glue"],
            "license": "apache-2.0",
        }
        assert "<h1>" in card.body_html
        assert "This is the body." in card.body_html

    def test_missing_front_matter_returns_empty_dict(self) -> None:
        text = "Just a body, no fences.\n"
        card = parse_card_metadata(text)
        assert card.front_matter == {}
        assert card.body_html.strip()  # rendered

    def test_broken_yaml_is_tolerated(self) -> None:
        text = "---\n: bad: yaml: : :\n---\nbody text\n"
        card = parse_card_metadata(text)
        assert card.front_matter == {}
        # Body still contains the literal text (mistune escapes HTML).
        assert "body text" in card.body_html

    def test_escapes_raw_html_to_prevent_xss(self) -> None:
        text = "Hello <script>alert(1)</script> world\n"
        card = parse_card_metadata(text)
        assert "<script>" not in card.body_html
        assert "&lt;script&gt;" in card.body_html

    def test_single_string_dataset_normalised_to_list(self) -> None:
        text = "---\ndatasets: glue\n---\nbody\n"
        card = parse_card_metadata(text)
        assert card.datasets == ["glue"]


class TestCardProperties:
    def test_task_and_base_model(self) -> None:
        card = CardMetadata(front_matter={"task": "summarization", "base_model": "bert-base"})
        assert card.task == "summarization"
        assert card.base_model == "bert-base"

    def test_license_property(self) -> None:
        card = CardMetadata(front_matter={"license": "mit"})
        assert card.license == "mit"

    def test_tags_and_language_default_to_empty_lists(self) -> None:
        card = CardMetadata(front_matter={})
        assert card.tags == []
        assert card.language == []
        assert card.datasets == []

    def test_tags_language_datasets_from_lists(self) -> None:
        card = CardMetadata(
            front_matter={
                "tags": ["nlp", "transformers"],
                "language": ["en", "de"],
                "datasets": ["glue", "sst2"],
            }
        )
        assert card.tags == ["nlp", "transformers"]
        assert card.language == ["en", "de"]
        assert card.datasets == ["glue", "sst2"]


class TestReadCard:
    async def test_returns_none_when_no_readme(
        self, tmp_data_dir: Path, reset_settings: None
    ) -> None:
        assert await read_card("ghost", "nope") is None

    async def test_returns_card_when_readme_present(
        self, tmp_data_dir: Path, reset_settings: None
    ) -> None:
        _seed_bare_repo_with_readme(
            tmp_data_dir,
            owner="alice",
            name="carded",
            readme_name="README.md",
            readme_text="---\nlicense: mit\n---\n# Hello\n",
        )
        card = await read_card("alice", "carded")
        assert isinstance(card, CardMetadata)
        assert card.license == "mit"
        assert "<h1>" in card.body_html
