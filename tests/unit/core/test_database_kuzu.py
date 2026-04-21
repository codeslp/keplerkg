from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from codegraphcontext.core.database_kuzu import KuzuDBManager, KuzuSessionWrapper
from codegraphcontext.tools.indexing.persistence.writer import GraphWriter
from codegraphcontext.tools.indexing.schema import create_graph_schema


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


def test_delete_repository_removes_variable_nodes() -> None:
    repo_path = Path("/tmp/repo").resolve()
    file_path = (repo_path / "a.php").resolve()
    file_data = {
        "path": str(file_path),
        "repo_path": str(repo_path),
        "lang": "php",
        "functions": [],
        "classes": [],
        "variables": [{"name": "$n", "line_number": 3, "lang": "php", "context": ""}],
        "imports": [],
        "function_calls": [],
        "is_dependency": False,
    }

    with TemporaryDirectory() as td:
        db_manager = KuzuDBManager(str(Path(td) / "db"))
        driver = db_manager.get_driver()
        create_graph_schema(driver, db_manager)
        writer = GraphWriter(driver)

        writer.add_repository_to_graph(repo_path)
        writer.add_file_to_graph(file_data, repo_path.name, {}, repo_path_str=str(repo_path))

        with driver.session() as session:
            before = session.run(
                "MATCH (v:Variable {name: '$n', path: $path, line_number: 3}) RETURN count(v) as c",
                path=str(file_path),
            ).single()["c"]
        assert before == 1

        writer.delete_repository_from_graph(str(repo_path))

        with driver.session() as session:
            after = session.run(
                "MATCH (v:Variable {name: '$n', path: $path, line_number: 3}) RETURN count(v) as c",
                path=str(file_path),
            ).single()["c"]
        assert after == 0


def test_write_inheritance_links_supports_csharp_interface_implementation() -> None:
    repo_path = Path("/tmp/repo").resolve()
    file_path = (repo_path / "a.cs").resolve()
    file_data = {
        "path": str(file_path),
        "lang": "c_sharp",
        "classes": [{"name": "MyClass", "line_number": 1, "bases": ["IMyInterface"], "lang": "c_sharp"}],
        "interfaces": [{"name": "IMyInterface", "line_number": 2, "bases": [], "lang": "c_sharp"}],
    }
    parsed_file = {
        "path": str(file_path),
        "repo_path": str(repo_path),
        "lang": "c_sharp",
        "functions": [],
        "classes": file_data["classes"],
        "interfaces": file_data["interfaces"],
        "variables": [],
        "imports": [],
        "function_calls": [],
        "is_dependency": False,
    }

    with TemporaryDirectory() as td:
        db_manager = KuzuDBManager(str(Path(td) / "db"))
        driver = db_manager.get_driver()
        create_graph_schema(driver, db_manager)
        writer = GraphWriter(driver)

        writer.add_repository_to_graph(repo_path)
        writer.add_file_to_graph(parsed_file, repo_path.name, {}, repo_path_str=str(repo_path))
        writer.write_inheritance_links([], [file_data], {})

        with driver.session() as session:
            count = session.run(
                """
                MATCH (c:Class {name: 'MyClass', path: $path})
                MATCH (i:Interface {name: 'IMyInterface'})
                MATCH (c)-[r:IMPLEMENTS]->(i)
                RETURN count(r) as c
                """,
                path=str(file_path),
            ).single()["c"]
        assert count == 1
