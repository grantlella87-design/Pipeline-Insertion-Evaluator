"""Makes the `pipelineinsertion` package importable from the scripts folder.

The diagnostic scripts are run directly (`python scripts/whatever.py`) rather
than as an installed package, so `src/` is not on sys.path. Importing this
module puts it there and re-exports the shared configuration:

    from _bootstrap import config

    df = pd.read_pickle(config.LAYER_CACHE_DIR / "main_lines.pkl.gz")

If the project is installed (`pip install -e .`) this is unnecessary and
`from pipelineinsertion import config` works directly.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pipelineinsertion import config

__all__ = ["SRC", "config"]
