import typer

from codegraphcontext_ext.cli import register_extensions
from codegraphcontext_ext.commands import COMMAND_MODULES
from codegraphcontext_ext.embeddings.providers import available_providers
from codegraphcontext_ext.io.schema_check import load_schema, schema_path


def test_register_extensions_registers_sync_check_embed_and_context():
    app = typer.Typer()

    register_extensions(app)

    assert [command.name for command in app.registered_commands] == [
        "advise",
        "audit",
        "blast-radius",
        "clusters",
        "drift-check",
        "sync-check",
        "embed",
        "health",
        "hotspots",
        "entrypoints",
        "execution-flow",
        "impact",
        "manifest",
        "repl",
        "search",
        "review-packet",
        "snapshot",
        "viz-embeddings",
        "viz-graph",
        "viz-dashboard",
        "viz-projector",
        "export-embeddings",
        "serve",
        "serve-localhost",
    ]


def test_command_modules_have_matching_schema_stubs():
    for module in COMMAND_MODULES:
        schema = load_schema(module.SCHEMA_FILE)
        assert schema_path(module.SCHEMA_FILE).is_file()
        assert schema["title"]
        assert schema.get("type") == "object" or "oneOf" in schema


def test_available_providers_match_the_spec_scaffold():
    assert available_providers() == ("local", "voyage", "openai")
