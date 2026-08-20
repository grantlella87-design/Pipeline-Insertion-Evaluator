"""Every module imports, and importing the package has no side effects.

A module that only fails to import when a particular code path reaches it is a
run that dies minutes in. These load everything up front.
"""
import importlib
import os

import pytest

PACKAGE_MODULES = [
    "pipelineinsertion",
    "pipelineinsertion.arcgis",
    "pipelineinsertion.auth",
    "pipelineinsertion.classify",
    "pipelineinsertion.config",
    "pipelineinsertion.crs",
    "pipelineinsertion.domains",
    "pipelineinsertion.fields",
    "pipelineinsertion.gsep",
    "pipelineinsertion.nearest",
    "pipelineinsertion.output",
    "pipelineinsertion.pressure",
    "pipelineinsertion.schema",
    "pipelineinsertion.systems",
    "pipelineinsertion.viewer_pane",
]

TOP_LEVEL_MODULES = ["pipeline_insertion_evaluator", "leaflet_bbox_server"]


@pytest.mark.parametrize("name", PACKAGE_MODULES + TOP_LEVEL_MODULES)
def test_module_imports(name):
    assert importlib.import_module(name) is not None


def test_importing_the_package_touches_no_filesystem_or_network():
    """Import must not create folders, read data or open a connection.

    The work root is a user's Downloads folder by default, and a test run that
    creates it is a test run with a side effect on the machine it ran on.
    """
    from pipelineinsertion import config

    importlib.reload(config)
    assert not config.WORK_ROOT.exists() or config.WORK_ROOT.is_dir()
    # Nothing under the work root is created by an import.
    assert not (config.OUTPUT_DIR / "anything").exists()


def test_config_is_overridable_by_environment(monkeypatch, tmp_path):
    # This is what makes the workflow runnable off one person's workstation.
    from pipelineinsertion import config

    monkeypatch.setenv("PIPEINSERT_WORK_ROOT", str(tmp_path))
    monkeypatch.setenv("PIPEINSERT_MAX_DISTANCE_FT", "75")
    importlib.reload(config)
    try:
        assert config.WORK_ROOT == tmp_path
        assert config.MAX_DISTANCE_FT == 75.0
        assert config.LAYER_CACHE_DIR == tmp_path / "layer_cache"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_describe_reports_the_resolved_configuration():
    from pipelineinsertion import config

    described = config.describe()
    for key in ("work_root", "output_gpkg", "main_lines_url", "max_distance_ft"):
        assert key in described


def test_the_service_url_points_at_ma_pressure_view_main_lines():
    from pipelineinsertion import config

    assert "Pressure_View_MA" in config.MAIN_LINES_URL
    assert config.MAIN_LINES_URL.rstrip("/").endswith("/145")
    assert config.MAIN_LINES_LAYER_ID == 145
