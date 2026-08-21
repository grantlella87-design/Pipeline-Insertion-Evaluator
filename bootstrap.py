"""Make a bare Python able to run this project: create .venv, install, re-exec.

`python run.py` on a fresh checkout used to get as far as the ArcGIS sign-in -
which is the slow, interactive part - and only then die on
`ModuleNotFoundError: No module named 'geopandas'`, having already made the
user authenticate for nothing. The dependency check now happens before anything
else runs.

What it does, when the interpreter it is running under cannot import what the
workflow needs:

1. creates `.venv` in the repository root, if there is not one already;
2. installs `requirements.txt` into it;
3. re-runs the original command with the venv's interpreter.

Nothing here imports anything outside the standard library, because the whole
point is that it runs on an interpreter where the dependencies are missing.
That includes not importing `pipelineinsertion` - `config.py` happens to be
stdlib-only today, and relying on that would make this file break the first
time somebody adds an import to it.

Run it on its own to set the environment up without starting a workflow:

    python bootstrap.py            create .venv and install into it
    python bootstrap.py --check    report what is missing, change nothing
    python bootstrap.py --force    reinstall even if nothing is missing

Two ways to opt out, for a machine that manages its own environment - a conda
install, a company-managed Python, a container that already has everything:

    python run.py --no-bootstrap
    set PIPEINSERT_NO_BOOTSTRAP=1
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR = REPO_ROOT / ".venv"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# The imports the workflow makes at module load. Checked by name rather than by
# importing them, because importing geopandas costs about a second and this runs
# on every single invocation.
#
# These are import names, not distribution names: the thing pip installs and the
# thing you import are not always spelled the same, and it is the import that
# fails at runtime.
REQUIRED_IMPORTS = ("geopandas", "pandas", "shapely", "pyproj", "requests", "keyring")

# Installed, but their absence is not what this checks for. `auth.make_session`
# imports truststore inside a try/except and carries on without it, so a
# missing one is not a reason to rebuild an environment - but it is very much a
# reason to have it installed. truststore injects the OS certificate store into
# TLS, which is what lets a request succeed on a corporate network whose proxy
# presents an internal CA that certifi has never heard of. It is in
# requirements.txt so the venv gets it; it is not here so that a machine
# without it still runs.
OPTIONAL_IMPORTS = ("truststore",)

# Set before re-exec so a venv that still cannot import what it needs reports
# that, rather than re-execing itself forever.
SENTINEL_ENV = "PIPEINSERT_BOOTSTRAPPED"
OPT_OUT_ENV = "PIPEINSERT_NO_BOOTSTRAP"


def venv_python(venv_dir=VENV_DIR):
    """The interpreter inside a venv, by platform layout."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def missing_imports(names=REQUIRED_IMPORTS):
    """Which of `names` this interpreter cannot import.

    `find_spec` rather than a real import: it answers the same question for a
    missing package and costs nothing. It will not catch a package that is
    installed but broken - a geopandas whose GDAL will not load, say - and that
    is deliberate. Reinstalling would not fix it, and the real ImportError says
    far more about it than this could.
    """
    missing = []
    for name in names:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            # find_spec raises rather than returning None when a parent package
            # is itself broken. Either way the import is not usable.
            missing.append(name)
    return missing


def missing_imports_in(python_exe, names=REQUIRED_IMPORTS):
    """Which of `names` another interpreter cannot import.

    Asked of the venv before running pip, so a second run skips the install
    entirely instead of paying for pip to tell us everything is satisfied.
    """
    probe = (
        "import importlib.util,sys;"
        "names=sys.argv[1:];"
        "print('\\n'.join(n for n in names "
        "if importlib.util.find_spec(n) is None))"
    )
    try:
        finished = subprocess.run(
            [str(python_exe), "-c", probe, *names],
            capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        # An interpreter that cannot be run at all is treated as satisfying
        # nothing, so the caller installs and then finds out properly.
        return list(names)
    if finished.returncode != 0:
        return list(names)
    return [line.strip() for line in finished.stdout.splitlines() if line.strip()]


def running_in(venv_dir=VENV_DIR):
    """True when this interpreter is running inside `venv_dir`.

    Compared on `sys.prefix`, which a venv points at itself, and not on the
    interpreter path. On macOS and Linux `.venv/bin/python` is a *symlink to
    the base interpreter*, so resolving both paths and comparing them made
    every Python that shared that base look like it was already inside the
    venv - which sent a plain `python run.py` down the "install in place"
    branch and ran pip on every single invocation.
    """
    try:
        return Path(sys.prefix).resolve() == Path(venv_dir).resolve()
    except OSError:
        return False


def create_venv(venv_dir=VENV_DIR, log=print):
    """Create the venv, unless it is already there.

    `--upgrade-deps` is deliberately not used: it upgrades pip on every single
    run, which on a slow or offline network is a long wait for nothing. pip is
    upgraded once, when the venv is first created.
    """
    if venv_python(venv_dir).is_file():
        return False

    log(f"Creating a virtual environment at {venv_dir}")
    log(f"  from {sys.executable}")
    finished = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)])
    if finished.returncode != 0 or not venv_python(venv_dir).is_file():
        raise RuntimeError(
            f"Could not create a virtual environment at {venv_dir}. "
            f"'{sys.executable} -m venv' exited {finished.returncode}. "
            f"On some Linux installs the venv module is a separate package "
            f"(python3-venv). To skip this step and manage the environment "
            f"yourself, set {OPT_OUT_ENV}=1.")
    return True


def install_requirements(venv_dir=VENV_DIR, upgrade_pip=False, log=print):
    """Install requirements.txt into the venv.

    Output is not captured. Installing geopandas and its geospatial stack takes
    minutes on a first run, and a silent terminal for that long reads as a
    hang - so pip's own progress is what the user watches.
    """
    python_exe = venv_python(venv_dir)
    if not REQUIREMENTS.is_file():
        raise RuntimeError(f"There is no {REQUIREMENTS} to install from.")

    if upgrade_pip:
        log("Upgrading pip in the new environment")
        subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade",
                        "pip", "--quiet"])

    log(f"Installing {REQUIREMENTS.name}. A first run downloads the geospatial "
        f"stack and takes a few minutes.")
    finished = subprocess.run([str(python_exe), "-m", "pip", "install",
                               "-r", str(REQUIREMENTS)])
    if finished.returncode != 0:
        raise RuntimeError(
            f"pip exited {finished.returncode} installing {REQUIREMENTS}. "
            f"The output above says why. To install by hand:\n"
            f"    {python_exe} -m pip install -r {REQUIREMENTS}")

    still_missing = missing_imports_in(python_exe)
    if still_missing:
        raise RuntimeError(
            f"pip reported success but {still_missing} still cannot be "
            f"imported from {python_exe}. This usually means a package "
            f"installed for a different Python version than the one running "
            f"the venv.")
    log("Dependencies installed.")


def reexec(argv, venv_dir=VENV_DIR, log=print):
    """Re-run the original command with the venv's interpreter.

    A subprocess rather than os.execv. On Windows execv does not replace the
    process the way it does elsewhere - the parent returns immediately and the
    shell prints its prompt over the child's still-running output - and this
    project is used from PowerShell.

    KeyboardInterrupt is caught because both processes are in the same console
    group: Ctrl+C at the map server reaches the child and this parent at once,
    and without this it prints a traceback on top of the child's clean exit.
    """
    python_exe = venv_python(venv_dir)
    command = [str(python_exe), *argv]
    log(f"Re-running with {python_exe}\n")

    environment = dict(os.environ)
    environment[SENTINEL_ENV] = "1"
    try:
        return subprocess.run(command, env=environment).returncode
    except KeyboardInterrupt:
        return 130


def ensure(argv=None, venv_dir=VENV_DIR, log=print):
    """Guarantee the dependencies, re-execing if that needed a different Python.

    Returns None when the current interpreter is already usable and the caller
    should carry on. Returns an exit code when the work was done in a
    subprocess and the caller should exit with it.
    """
    if os.environ.get(OPT_OUT_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return None

    missing = missing_imports()
    if not missing:
        return None

    if os.environ.get(SENTINEL_ENV):
        # Already re-exec'd once and the packages are still not importable.
        # Going round again would loop forever.
        raise RuntimeError(
            f"{missing} are still missing after the environment was set up. "
            f"Install them by hand:\n"
            f"    {venv_python(venv_dir)} -m pip install -r {REQUIREMENTS}")

    if running_in(venv_dir):
        # Inside the project venv, so there is no other interpreter to move to.
        # Installing in place is the whole fix.
        log(f"This environment is missing {missing}.")
        install_requirements(venv_dir, log=log)
        raise SystemExit(reexec(argv if argv is not None else sys.argv,
                                venv_dir, log=log))

    # Whether the venv needs work is decided before anything is logged, so a
    # repeat run - the common case, once the environment exists - prints one
    # line about which interpreter it moved to, rather than a setup banner for
    # setup that is not happening.
    needs_setup = (not venv_python(venv_dir).is_file()
                   or missing_imports_in(venv_python(venv_dir)))
    if needs_setup:
        log("=== First-run setup ===")
        log(f"{sys.executable}")
        log(f"cannot import {missing}, so the project's own environment is "
            f"being built. This happens once.")
        created = create_venv(venv_dir, log=log)
        install_requirements(venv_dir, upgrade_pip=created, log=log)

    return reexec(argv if argv is not None else sys.argv, venv_dir, log=log)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--check" in argv:
        missing = missing_imports()
        print(f"Interpreter: {sys.executable}")
        print(f"Virtual environment: {venv_dir_status()}")
        if missing:
            print(f"Missing here: {missing}")
            print("Run: python bootstrap.py")
            return 1
        print("Everything the workflow imports is available.")
        return 0

    created = create_venv(VENV_DIR)
    force = "--force" in argv
    if force or created or missing_imports_in(venv_python()):
        install_requirements(VENV_DIR, upgrade_pip=created)
    else:
        print("Nothing to install; the environment is already complete.")
    print(f"\nReady. Run the workflow with:\n    {venv_python()} run.py")
    print("or just 'python run.py' - it will use this environment "
          "automatically.")
    return 0


def venv_dir_status():
    return str(VENV_DIR) if venv_python().is_file() else f"{VENV_DIR} (not created)"


if __name__ == "__main__":
    sys.exit(main())
