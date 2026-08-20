"""One command: sign in, download, evaluate, write, and open the map.

    python run.py                    everything, then serve the map
    python run.py --no-view          stop after the GeoPackage
    python run.py --view-only        just serve the map
    python run.py --refresh          ignore the layer cache
    python run.py --port 8800        serve on another port

The evaluator is src/pipeline_insertion_evaluator.py and the map is
src/leaflet_bbox_server.py, which draws the Lower Pressure systems, the Other
Pressure systems they could be inserted into, the shortest connection path
between each pair and the candidates that passed both final tests.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
for folder in ("src", "scripts"):
    path = str(REPO_ROOT / folder)
    if path not in sys.path:
        sys.path.insert(0, path)

from pipelineinsertion import config
from pipelineinsertion.output import fail, log, step, warn


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-view", action="store_true",
                        help="Stop after writing the GeoPackage.")
    parser.add_argument("--view-only", action="store_true",
                        help="Skip the evaluation and serve the map.")
    parser.add_argument("--refresh", action="store_true",
                        help="Ignore the layer cache and re-download.")
    parser.add_argument("--port", type=int, default=None,
                        help="Port for the map. Default: the map server's own.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Serve the map without opening a browser.")
    parser.add_argument("--skip-signin-check", action="store_true",
                        help="Do not verify the token before the long stages.")
    return parser.parse_args(argv)


def check_signin():
    """Prove the token works before anything long starts.

    A sign-in problem otherwise surfaces part-way through a download, after
    minutes of waiting.
    """
    step("Checking the ArcGIS token")
    from pipelineinsertion import auth

    session = auth.make_session()
    if not auth.get_arcgis_token(session):
        fail("No ArcGIS token. Run: python scripts/arcgis_signin.py --check")

    count = auth.authenticated_count(session, config.MAIN_LINES_URL)
    if count is None:
        fail("The token was rejected by the service. "
             "Run: python scripts/arcgis_signin.py --force")
    log(f"Token accepted. Main Lines reports {count:,} features.")


def run_workflow(refresh):
    step("Running the insertion evaluator")
    if refresh:
        os.environ["FORCE_LAYER_REFRESH"] = "1"
        log("FORCE_LAYER_REFRESH=1: the layer cache will be ignored.")

    import pipeline_insertion_evaluator as workflow

    workflow.main()
    return Path(config.OUTPUT_GPKG)


def serve_map(port, open_browser):
    """Serve the map with src/leaflet_bbox_server.py.

    A layer with no source is empty and says why, both here and on the page, so
    a fresh checkout with no GeoPackage still gets a map rather than a
    traceback.
    """
    step("Serving the map")
    import leaflet_bbox_server as map_server

    map_server.serve(port=port or map_server.PORT, open_browser=open_browser)


def main(argv=None):
    args = parse_args(argv)

    if args.no_view and args.view_only:
        fail("--no-view and --view-only ask for opposite things.")

    log("=== LPP GSEP Pipeline Insertion Evaluator ===")
    log(f"GeoPackage: {config.OUTPUT_GPKG}")
    log(f"Layer cache: {config.LAYER_CACHE_DIR}")

    if not args.view_only:
        if not args.skip_signin_check:
            check_signin()
        gpkg = run_workflow(args.refresh)
        if not gpkg.exists():
            fail(f"The workflow finished but {gpkg} is not there.")
    elif not Path(config.OUTPUT_GPKG).exists():
        warn(f"--view-only, but there is no GeoPackage at {config.OUTPUT_GPKG}. "
             "The map will draw, with every layer empty, until the workflow has "
             "run: python run.py --no-view")

    if args.no_view:
        log("Done. See the map later with: python run.py --view-only")
        return 0

    serve_map(args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
