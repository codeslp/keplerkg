from pathlib import Path

import pytest

from codegraphcontext_ext.commands.sync_check import (
    _parse_toml_string,
    _remote_label,
    _source_checkout_from_project_config,
)


@pytest.mark.parametrize(
    "remote_url, expected",
    [
        (
            "https://github.com/KeplerKG/KeplerKG.git",
            "KeplerKG/KeplerKG",
        ),
        ("https://github.com/codeslp/cgraph", "codeslp/cgraph"),
        ("https://github.com/codeslp/cgraph/", "codeslp/cgraph"),
        ("git@github.com:codeslp/cgraph.git", "codeslp/cgraph"),
        ("ssh://git@github.com/codeslp/cgraph.git", "codeslp/cgraph"),
        ("single-segment", "single-segment"),
    ],
)
def test_remote_label_normalizes_supported_url_shapes(remote_url, expected):
    assert _remote_label(remote_url) == expected


@pytest.mark.parametrize(
    "raw_value, expected",
    [
        ("", ""),
        ('"/srv/cgraph"', "/srv/cgraph"),
        ("'/srv/cgraph'", "/srv/cgraph"),
        ("/srv/cgraph", "/srv/cgraph"),
    ],
)
def test_parse_toml_string_handles_quoted_and_bare_values(raw_value, expected):
    assert _parse_toml_string(raw_value) == expected


def test_source_checkout_from_project_config_returns_path_under_cgraph_section(tmp_path):
    project_toml = tmp_path / "project.toml"
    project_toml.write_text(
        '[cgraph]\nsource_checkout = "/srv/cgraph"\n',
        encoding="utf-8",
    )
    assert _source_checkout_from_project_config(project_toml) == Path("/srv/cgraph")


def test_source_checkout_from_project_config_ignores_other_sections(tmp_path):
    project_toml = tmp_path / "project.toml"
    project_toml.write_text(
        '[other]\nsource_checkout = "/nope"\n[cgraph]\nsource_checkout = "/yep"\n',
        encoding="utf-8",
    )
    assert _source_checkout_from_project_config(project_toml) == Path("/yep")


def test_source_checkout_from_project_config_returns_none_when_key_absent(tmp_path):
    project_toml = tmp_path / "project.toml"
    project_toml.write_text('[cgraph]\nother_key = "x"\n', encoding="utf-8")
    assert _source_checkout_from_project_config(project_toml) is None


def test_source_checkout_from_project_config_strips_trailing_comments(tmp_path):
    project_toml = tmp_path / "project.toml"
    project_toml.write_text(
        '[cgraph]\nsource_checkout = "/srv/cgraph"  # dev fork\n',
        encoding="utf-8",
    )
    assert _source_checkout_from_project_config(project_toml) == Path("/srv/cgraph")


def test_source_checkout_from_project_config_expands_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project_toml = tmp_path / "project.toml"
    project_toml.write_text(
        '[cgraph]\nsource_checkout = "~/cgraph-fork"\n',
        encoding="utf-8",
    )
    assert _source_checkout_from_project_config(project_toml) == tmp_path / "cgraph-fork"
