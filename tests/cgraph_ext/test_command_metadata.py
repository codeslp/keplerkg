from codegraphcontext_ext.commands import COMMAND_MODULES
from codegraphcontext_ext.io.schema_check import schema_path

EXPECTED_METADATA = {
    "advise": ("advise", "advise.json"),
    "blast_radius": ("blast-radius", "blast-radius.json"),
    "context": ("context", "context.json"),
    "drift_check": ("drift-check", "drift-check.json"),
    "review_packet": ("review-packet", "review-packet.json"),
    "sync_check": ("sync-check", "sync-check.json"),
}


def test_every_command_module_declares_expected_metadata_constants():
    for module in COMMAND_MODULES:
        short_name = module.__name__.rsplit(".", 1)[-1]
        expected_name, expected_schema = EXPECTED_METADATA[short_name]
        assert module.COMMAND_NAME == expected_name
        assert module.SCHEMA_FILE == expected_schema
        assert isinstance(module.SUMMARY, str) and module.SUMMARY


def test_every_command_schema_file_exists_on_disk():
    for module in COMMAND_MODULES:
        assert schema_path(module.SCHEMA_FILE).is_file()


def test_expected_metadata_covers_every_loaded_command_module():
    loaded_short_names = {module.__name__.rsplit(".", 1)[-1] for module in COMMAND_MODULES}
    assert loaded_short_names == set(EXPECTED_METADATA.keys())
