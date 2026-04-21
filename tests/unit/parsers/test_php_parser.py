from __future__ import annotations

from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codegraphcontext.tools.languages.php import PhpTreeSitterParser
from codegraphcontext.utils.tree_sitter_manager import get_tree_sitter_manager


@pytest.fixture(scope="module")
def parser() -> PhpTreeSitterParser:
    manager = get_tree_sitter_manager()
    wrapper = MagicMock()
    wrapper.language_name = "php"
    wrapper.language = manager.get_language_safe("php")
    wrapper.parser = manager.create_parser("php")
    return PhpTreeSitterParser(wrapper)


def test_php_parser_dedupes_variables_sharing_name_and_line(parser: PhpTreeSitterParser) -> None:
    fixture = Path("tests/fixtures/sample_projects/sample_project_php/generators_iterators.php")
    parsed = parser.parse(fixture)

    counts = Counter((var["name"], var["line_number"]) for var in parsed["variables"])
    duplicates = {key: count for key, count in counts.items() if count > 1}

    assert duplicates == {}
