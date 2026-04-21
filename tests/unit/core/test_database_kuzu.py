from __future__ import annotations

from unittest.mock import MagicMock

from codegraphcontext.core.database_kuzu import KuzuSessionWrapper


def test_translate_query_dedupes_unwind_batch_rows_sharing_uid() -> None:
    session = KuzuSessionWrapper(MagicMock())
    query = """
        UNWIND $batch AS row
        MERGE (n:Variable {name: row.name, path: $file_path, line_number: row.line_number})
        SET n += row
    """
    params = {
        "batch": [
            {"name": "$n", "line_number": 3, "context": ""},
            {"name": "$n", "line_number": 3, "context": ""},
        ],
        "file_path": "/repo/a.php",
    }

    _translated_query, translated_params = session._translate_query(query, params)

    assert translated_params["batch"] == [
        {"name": "$n", "line_number": 3, "context": "", "uid": "$n/repo/a.php3"}
    ]
