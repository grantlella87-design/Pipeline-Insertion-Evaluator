"""Shared fixtures, and the sys.path setup the tests run under.

The package lives in `src/` and is not installed for a test run, so every test
module needs `src/` on the path. Doing it here means no test file starts with
three lines of path manipulation.
"""
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


@pytest.fixture
def restore_config():
    """Restore a config attribute after a test that changes one.

    Thresholds are module attributes read at call time, which is what lets a
    test prove a rule follows its configured value rather than a literal. The
    change has to be undone or it leaks into every later test.

        def test_something(restore_config):
            restore_config("MAX_DISTANCE_FT", 10.0)
    """
    from pipelineinsertion import config

    saved = {}

    def set_value(name, value):
        if name not in saved:
            saved[name] = getattr(config, name)
        setattr(config, name, value)

    yield set_value

    for name, value in saved.items():
        setattr(config, name, value)
