"""kkg export-embeddings: dump embedding vectors as TSVs for projector.tensorflow.org."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..embeddings.fetch import fetch_embedded_nodes
from ..embeddings.runtime import probe_backend_support
from ..io.json_stdout import emit_json
from ..io.kuzu import get_kuzu_connection
from ..project import PROJECT_OPTION_HELP, activate_project

COMMAND_NAME = "export-embeddings"
SCHEMA_FILE = "context.json"
SUMMARY = "Export embedding vectors as TSVs for TF Embedding Projector."


def _sanitize_tsv_cell(value: Any) -> str:
    """Strip tab/newline so a metadata cell can't break the TSV grid."""
    if value is None:
        return ""
    s = str(value)
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def _write_vectors_tsv(path: Path, nodes: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for n in nodes:
            f.write("\t".join(f"{v:.6f}" for v in n["embedding"]))
            f.write("\n")


def _write_metadata_tsv(path: Path, nodes: list[dict[str, Any]]) -> None:
    # Projector requires a header row when metadata has >1 column.
    with path.open("w", encoding="utf-8") as f:
        f.write("name\ttype\tpath\tline\n")
        for n in nodes:
            f.write(
                "\t".join(
                    _sanitize_tsv_cell(v)
                    for v in (n["name"], n["type"], n["path"], n["line"])
                )
            )
            f.write("\n")


def export_embeddings_command(
    prefix: str = typer.Option(
        "cgraph-embeddings",
        "--prefix", "-p",
        help="Output file prefix.  Emits <prefix>.vectors.tsv and <prefix>.metadata.tsv.",
    ),
    output_dir: Optional[str] = typer.Option(
        None,
        "--output-dir", "-d",
        help="Directory for the output files.  Defaults to the current working directory.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        help=PROJECT_OPTION_HELP,
    ),
) -> None:
    """Export embeddings as TSV files for projector.tensorflow.org."""
    activate_project(project)

    backend_payload = probe_backend_support()
    if not backend_payload["ok"]:
        typer.echo(emit_json(backend_payload))
        raise typer.Exit(code=1)

    conn = get_kuzu_connection()
    nodes = fetch_embedded_nodes(conn)

    if not nodes:
        typer.echo(emit_json({
            "ok": False,
            "kind": "no_embeddings",
            "detail": "No embedded nodes found. Run `kkg embed` first.",
        }))
        raise typer.Exit(code=1)

    out_dir = Path(output_dir) if output_dir else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    vectors_path = out_dir / f"{prefix}.vectors.tsv"
    metadata_path = out_dir / f"{prefix}.metadata.tsv"

    _write_vectors_tsv(vectors_path, nodes)
    _write_metadata_tsv(metadata_path, nodes)

    print(
        f"Wrote {len(nodes)} vectors to {vectors_path}\n"
        f"Wrote metadata to {metadata_path}\n"
        f"\n"
        f"Privacy note: uploading these files to projector.tensorflow.org\n"
        f"publishes function names and file paths to a Google-hosted tool.\n"
        f"Fine for OSS, worth considering for private codebases.",
        file=sys.stderr,
    )

    typer.echo(emit_json({
        "ok": True,
        "kind": "export_embeddings",
        "nodes": len(nodes),
        "vectors_path": str(vectors_path.resolve()),
        "metadata_path": str(metadata_path.resolve()),
        "upload_url": "https://projector.tensorflow.org/",
    }))
    raise typer.Exit(code=0)
