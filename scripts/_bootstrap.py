"""Makes the `pipelineinsertion` package importable from the scripts folder.

Not to be confused with `bootstrap.py` in the repository root, which is a
different job: that one creates the virtual environment and installs the
dependencies. This one is only about `sys.path`. Importing it also runs that
one, so a script run on a bare Python sets itself up the same way `run.py`
does - `scripts/arcgis_signin.py` needs `requests` and `keyring`, and failing
on a missing import is no more useful here than it is there.

    from _bootstrap import config

    df = pd.read_pickle(config.LAYER_CACHE_DIR / "main_lines.pkl.gz")

If the project is installed (`pip install -e .`) this is unnecessary and
`from pipelineinsertion import config` works directly.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The repository root, so `import bootstrap` finds the venv bootstrap. A script
# is run as `python scripts/whatever.py`, which puts `scripts/` on the path but
# not the root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bootstrap as _venv_bootstrap

_BOOTSTRAP_EXIT = _venv_bootstrap.ensure(sys.argv)
if _BOOTSTRAP_EXIT is not None:
    # The script has already run to completion in the venv's interpreter.
    raise SystemExit(_BOOTSTRAP_EXIT)

from pipelineinsertion import config

__all__ = ["SRC", "REPO_ROOT", "config"]
