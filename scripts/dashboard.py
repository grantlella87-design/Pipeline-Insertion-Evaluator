"""Rebuild the dashboard from an existing GeoPackage, without re-running.

    python scripts/dashboard.py             # write it beside the GeoPackage
    python scripts/dashboard.py --open      # and open it in a browser
    python scripts/dashboard.py --out x.html

The workflow writes one at the end of every run. This is for the case where the
GeoPackage is already there - a colleague's copy, or a run from last week - and
re-downloading the whole layer to look at a summary would be absurd.
"""
import argparse
import sys

from _bootstrap import config

from pipelineinsertion import dashboard
from pipelineinsertion.output import log


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpkg", default=None,
                        help=f"GeoPackage to read. Default: {config.OUTPUT_GPKG}")
    parser.add_argument("--out", default=None,
                        help="Where to write the HTML. Default: beside the GeoPackage.")
    parser.add_argument("--open", action="store_true",
                        help="Open it in a browser when it is written.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    target = dashboard.build(gpkg=args.gpkg, target=args.out)
    if args.open:
        import webbrowser

        webbrowser.open(target.as_uri())
        log("Opened in your browser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
