"""Tests for kkg export-embeddings — TSV export for TF Embedding Projector."""

from unittest.mock import patch

from typer.testing import CliRunner

from .conftest import (
    FunctionOnlyConn,
    build_ext_app,
    extract_last_json,
    mark_kuzu_backend_available,
)

runner = CliRunner()


def _patched_conn(conn):
    """Patch export_embeddings' kuzu accessor for the duration of a test."""
    return patch(
        "codegraphcontext_ext.commands.export_embeddings.get_kuzu_connection",
        return_value=conn,
    )


def test_export_embeddings_registered():
    app = build_ext_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "export-embeddings" in names


def test_export_embeddings_no_embeddings_returns_typed_error(monkeypatch):
    mark_kuzu_backend_available(monkeypatch)

    with _patched_conn(FunctionOnlyConn([])):
        result = runner.invoke(build_ext_app(), ["export-embeddings"])

    assert result.exit_code == 1
    payload = extract_last_json(result.output)
    assert payload["kind"] == "no_embeddings"


def test_export_embeddings_writes_both_tsv_files(monkeypatch, tmp_path):
    mark_kuzu_backend_available(monkeypatch)

    emb_rows = [
        ("uid1", "foo", "a.py", 1, [0.1, 0.2, 0.3, 0.4]),
        ("uid2", "Bar", "b.py", 42, [0.5, 0.6, 0.7, 0.8]),
    ]

    with _patched_conn(FunctionOnlyConn(emb_rows)):
        result = runner.invoke(
            build_ext_app(),
            ["export-embeddings", "--prefix", "run1", "--output-dir", str(tmp_path)],
        )

    assert result.exit_code == 0, result.output
    payload = extract_last_json(result.output)
    assert payload["ok"] is True
    assert payload["kind"] == "export_embeddings"
    assert payload["nodes"] == 2
    assert payload["upload_url"] == "https://projector.tensorflow.org/"

    vectors_path = tmp_path / "run1.vectors.tsv"
    metadata_path = tmp_path / "run1.metadata.tsv"
    assert vectors_path.exists()
    assert metadata_path.exists()

    # Vector file: one row per embedding, tab-separated floats, no header.
    vector_lines = vectors_path.read_text().strip().splitlines()
    assert len(vector_lines) == 2
    first_row = vector_lines[0].split("\t")
    assert len(first_row) == 4
    assert [float(x) for x in first_row] == [0.1, 0.2, 0.3, 0.4]

    # Metadata file: header row + one row per vector in the same order.
    meta_lines = metadata_path.read_text().strip().splitlines()
    assert meta_lines[0] == "name\ttype\tpath\tline"
    assert meta_lines[1].split("\t") == ["foo", "Function", "a.py", "1"]
    assert meta_lines[2].split("\t") == ["Bar", "Function", "b.py", "42"]


def test_export_embeddings_sanitizes_tab_and_newline_in_metadata(monkeypatch, tmp_path):
    """A function name with a literal tab/newline must not break the TSV grid."""
    mark_kuzu_backend_available(monkeypatch)

    emb_rows = [("uid1", "foo\twith\ttabs", "a\nb.py", 1, [0.1, 0.2])]

    with _patched_conn(FunctionOnlyConn(emb_rows)):
        result = runner.invoke(
            build_ext_app(),
            ["export-embeddings", "--prefix", "dirty", "--output-dir", str(tmp_path)],
        )

    assert result.exit_code == 0, result.output

    meta_lines = (tmp_path / "dirty.metadata.tsv").read_text().strip().splitlines()
    assert len(meta_lines) == 2
    cells = meta_lines[1].split("\t")
    assert len(cells) == 4
    assert "\t" not in cells[0] and "\n" not in cells[0]
    assert "\n" not in cells[2]


def test_export_embeddings_defaults_to_cwd(monkeypatch, tmp_path):
    """Omitting --output-dir writes to the current working directory."""
    mark_kuzu_backend_available(monkeypatch)
    monkeypatch.chdir(tmp_path)

    with _patched_conn(FunctionOnlyConn([("uid1", "foo", "a.py", 1, [0.1, 0.2])])):
        result = runner.invoke(build_ext_app(), ["export-embeddings", "--prefix", "cwd-test"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "cwd-test.vectors.tsv").exists()
    assert (tmp_path / "cwd-test.metadata.tsv").exists()


def test_export_embeddings_emits_privacy_warning_on_stderr(monkeypatch, tmp_path):
    """Users uploading to projector.tensorflow.org should see the privacy note."""
    mark_kuzu_backend_available(monkeypatch)

    with _patched_conn(FunctionOnlyConn([("uid1", "foo", "a.py", 1, [0.1, 0.2])])):
        result = runner.invoke(
            build_ext_app(),
            ["export-embeddings", "--prefix", "warn", "--output-dir", str(tmp_path)],
        )

    assert result.exit_code == 0, result.output
    # runner.output combines stdout + stderr; that's enough to assert the warning surfaced.
    assert "Privacy note" in result.output
    assert "projector.tensorflow.org" in result.output
