"""Tests for the Phase 5.7 clusters command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import jsonschema
import typer
from typer.testing import CliRunner

from codegraphcontext_ext.commands.clusters import (
    COMMAND_NAME,
    SCHEMA_FILE,
    SUMMARY,
    _DEFAULT_MAX_SEMANTIC_NODES,
    _DEFAULT_SEMANTIC_THRESHOLD,
    build_clusters_payload,
    clusters_command,
)
from codegraphcontext_ext.io.schema_check import schema_path
from codegraphcontext_ext.project import ProjectTarget

runner = CliRunner()


def _sample_community_data() -> dict:
    return {
        "communities": [
            {
                "id": 0,
                "size": 2,
                "members": [
                    {"uid": "a", "name": "alpha", "path": "src/a.py", "type": "Function"},
                    {"uid": "b", "name": "beta", "path": "src/b.py", "type": "Function"},
                ],
            },
            {
                "id": 1,
                "size": 1,
                "members": [
                    {"uid": "c", "name": "gamma", "path": "src/c.py", "type": "Function"},
                ],
            },
        ],
        "edges": [
            {
                "source": "a",
                "target": "b",
                "type": "CALLS",
                "provenance": "extracted",
                "confidence": 1.0,
                "community": 0,
            },
        ],
        "cross_edges": [
            {
                "source": "b",
                "target": "c",
                "type": "CALLS",
                "provenance": "extracted",
                "source_community": 0,
                "target_community": 1,
            },
        ],
        "stats": {
            "total_nodes": 3,
            "total_edges": 2,
            "communities": 2,
            "structural_edges": 2,
            "semantic_edges": 0,
            "cross_community_edges": 1,
        },
    }


def _clusters_app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _root() -> None:
        return None

    app.command(name=COMMAND_NAME, help=SUMMARY)(clusters_command)
    return app


def _load_schema() -> dict:
    return json.loads(schema_path("clusters.json").read_text())


def test_command_metadata():
    assert COMMAND_NAME == "clusters"
    assert SCHEMA_FILE == "clusters.json"
    assert isinstance(SUMMARY, str) and SUMMARY


def test_default_parameters():
    assert _DEFAULT_SEMANTIC_THRESHOLD == 0.85
    assert _DEFAULT_MAX_SEMANTIC_NODES == 2000


def test_build_clusters_payload_success():
    sample = _sample_community_data()
    conn = object()

    with patch(
        "codegraphcontext_ext.commands.clusters.fetch_community_data",
        return_value=sample,
    ) as mock_fetch:
        payload = build_clusters_payload(
            conn=conn,
            semantic_threshold=0.9,
            max_semantic_nodes=321,
            project="demo-project",
        )

    mock_fetch.assert_called_once_with(
        conn,
        semantic_threshold=0.9,
        max_semantic_nodes=321,
    )
    assert payload["ok"] is True
    assert payload["kind"] == "clusters"
    assert payload["project"] == "demo-project"
    assert payload["communities"] == sample["communities"]
    assert payload["edges"] == sample["edges"]
    assert payload["cross_edges"] == sample["cross_edges"]
    assert payload["stats"] == sample["stats"]
    assert payload["parameters"] == {
        "semantic_threshold": 0.9,
        "max_semantic_nodes": 321,
    }
    assert payload["advisories"] == []


def test_build_clusters_payload_no_graph():
    payload = build_clusters_payload(conn=None, project="demo-project")

    assert payload["ok"] is True
    assert payload["kind"] == "clusters"
    assert payload["project"] == "demo-project"
    assert payload["communities"] == []
    assert payload["edges"] == []
    assert payload["cross_edges"] == []
    assert payload["stats"]["total_nodes"] == 0
    assert any(a["kind"] == "no_graph" for a in payload["advisories"])


def test_build_clusters_payload_failure_sets_error():
    with patch(
        "codegraphcontext_ext.commands.clusters.fetch_community_data",
        side_effect=RuntimeError("boom"),
    ):
        payload = build_clusters_payload(conn=object(), project="demo-project")

    assert payload["ok"] is False
    assert payload["kind"] == "clusters"
    assert payload["project"] == "demo-project"
    assert payload["error"] == "Community detection failed: boom"
    assert payload["communities"] == []


def test_schema_validation_success():
    with patch(
        "codegraphcontext_ext.commands.clusters.fetch_community_data",
        return_value=_sample_community_data(),
    ):
        payload = build_clusters_payload(conn=object(), project="demo-project")

    jsonschema.validate(payload, _load_schema())


def test_schema_validation_no_graph():
    payload = build_clusters_payload(conn=None, project="demo-project")
    jsonschema.validate(payload, _load_schema())


def test_cli_basic():
    app = _clusters_app()
    target = ProjectTarget(slug="demo-project", db_path=Path("/tmp/demo/kuzudb"), source="cli")

    with patch(
        "codegraphcontext_ext.commands.clusters.activate_project",
        return_value=target,
    ), patch(
        "codegraphcontext_ext.commands.clusters.get_kuzu_connection",
        return_value=object(),
    ), patch(
        "codegraphcontext_ext.commands.clusters.fetch_community_data",
        return_value=_sample_community_data(),
    ):
        result = runner.invoke(
            app,
            [
                "clusters",
                "--semantic-threshold",
                "0.9",
                "--max-semantic-nodes",
                "321",
                "--project",
                "demo-project",
            ],
        )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["kind"] == "clusters"
    assert payload["project"] == "demo-project"
    assert payload["parameters"] == {
        "semantic_threshold": 0.9,
        "max_semantic_nodes": 321,
    }
