from pathlib import Path
import warnings

from setuptools import find_packages
from setuptools.config.pyprojecttoml import read_configuration


def test_pyproject_package_discovery_includes_cgraph_extension_packages():
    repo_root = Path(__file__).resolve().parents[2]
    pyproject_path = repo_root / "pyproject.toml"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        config = read_configuration(pyproject_path, expand=False)

    package_find = config["tool"]["setuptools"]["packages"]["find"]
    includes = package_find["include"]
    where = package_find["where"]

    assert "codegraphcontext_ext*" in includes

    discovered = set(
        find_packages(
            where=str(repo_root / where[0]),
            include=includes,
        )
    )
    ext_packages = set(
        find_packages(
            where=str(repo_root / "src"),
            include=["codegraphcontext_ext*"],
        )
    )

    assert "codegraphcontext_ext" in discovered
    assert ext_packages
    assert ext_packages.issubset(discovered)
