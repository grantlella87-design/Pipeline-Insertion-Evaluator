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

On the office network Zscaler intercepts outbound TLS, so pip cannot reach
PyPI directly - the connection is reset before it starts. The proxy is detected
and used automatically, and the Windows trust store is handed to pip so the
re-signed certificates verify. See the "Corporate network" section below.

Run it on its own to set the environment up without starting a workflow:

    python bootstrap.py            create .venv and install into it
    python bootstrap.py --check    report what is missing, change nothing
    python bootstrap.py --network  report what it thinks of the network
    python bootstrap.py --force    reinstall even if nothing is missing

Two ways to opt out, for a machine that manages its own environment - a conda
install, a company-managed Python, a container that already has everything:

    python run.py --no-bootstrap
    set PIPEINSERT_NO_BOOTSTRAP=1

To override the network detection:

    set PIPEINSERT_PIP_PROXY=http://proxy:port   force this proxy
    set PIPEINSERT_PIP_PROXY=                    force a direct connection
    set PIPEINSERT_ZSCALER_PROXY=http://...      look for a different Zscaler
    set PIPEINSERT_PIP_CERT=C:\\path\\to\\ca.pem   verify against this bundle
"""
import importlib.util
import os
import socket
import ssl
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
VENV_DIR = REPO_ROOT / ".venv"
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# --- Corporate network -------------------------------------------------------
#
# On the office network Zscaler intercepts outbound TLS, and pip's connection to
# PyPI is reset before it gets anywhere:
#
#     WARNING: Retrying ... after connection broken by
#     'ProtocolError('Connection aborted.',
#      ConnectionResetError(10054, 'An existing connection was forcibly closed
#      by the remote host'))': /simple/pip/
#
# Two separate things have to be dealt with, and fixing only the first leaves a
# failure that looks unrelated to it:
#
#   the route  pip has to go through the Zscaler proxy rather than straight out
#   the trust  once it does, the certificate PyPI appears to present is signed
#              by Zscaler's own CA, which certifi has never heard of - so a
#              working proxy turns a connection reset into an SSL error
#
# The proxy is detected rather than assumed, because the same checkout is used
# off the corporate network, where forcing a proxy that cannot be reached would
# break a run that would otherwise have worked. See `detect_zscaler`.
DEFAULT_ZSCALER_PROXY = "http://zscaler.nationalgrid.com:80"

PROXY_ENV = "PIPEINSERT_PIP_PROXY"
ZSCALER_PROXY_ENV = "PIPEINSERT_ZSCALER_PROXY"
CA_BUNDLE_ENV = "PIPEINSERT_PIP_CERT"

# Proxy variables pip reads on its own. When one of these is set the user has
# already said how to get out, and adding --proxy on top would override a
# deliberate choice.
STANDARD_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                      "ALL_PROXY", "all_proxy")

# How long to wait when testing whether the proxy is reachable. Long enough for
# a corporate DNS lookup, short enough that someone working from home does not
# sit through it.
PROXY_PROBE_TIMEOUT_SECONDS = 3.0

# Windows process names that mean the Zscaler client is running.
ZSCALER_PROCESS_NAMES = ("ZSATray.exe", "ZSATunnel.exe", "ZSAService.exe",
                         "ZSAUpm.exe", "ZSAUpdater.exe")

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


# --- Detecting Zscaler -------------------------------------------------------


def tcp_reachable(host, port, timeout=PROXY_PROBE_TIMEOUT_SECONDS):
    """Whether a TCP connection to host:port can be opened.

    This, rather than "is the user in the office", is the question that
    actually matters. The Zscaler client tunnels from home as well, and someone
    in the office on a guest network is not behind it - so what decides whether
    to use the proxy is whether the proxy answers.
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, OverflowError):
        # Refused, unroutable, DNS failure, timeout, or a nonsense port.
        return False


def split_host_port(url, default_port=80):
    """(host, port) from a proxy URL, with or without a scheme."""
    text = str(url).strip()
    for scheme in ("http://", "https://"):
        if text.lower().startswith(scheme):
            text = text[len(scheme):]
            break
    text = text.split("/", 1)[0]
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    if ":" in text:
        host, _, port = text.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return text, default_port
    return text, default_port


def normalise_proxy(url):
    """A proxy URL pip will accept, adding the scheme when it is missing."""
    text = str(url).strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text


def windows_root_certificates():
    """Every certificate in the Windows ROOT store, as DER bytes.

    `ssl.enum_certificates` is Windows-only and stdlib, which is what makes
    this usable from here - reading the machine's trusted roots without needing
    a package installed first is precisely the chicken-and-egg this file
    exists to avoid.
    """
    if not hasattr(ssl, "enum_certificates"):
        return []
    found = []
    for store in ("ROOT", "CA"):
        try:
            for cert_bytes, encoding, trust in ssl.enum_certificates(store):
                if encoding == "x509_asn" and trust is not False:
                    found.append(cert_bytes)
        except (OSError, PermissionError, ValueError):
            # A store that cannot be read is skipped; the other may still work.
            continue
    return found


def zscaler_certificate_names():
    """Zscaler roots installed on this machine, by subject.

    IT pushing a Zscaler root into the trust store is the strongest available
    signal that this machine's traffic is being intercepted - stronger than a
    reachable proxy, which only says the route exists.
    """
    # Matched against the raw DER rather than a parsed subject. Parsing one
    # properly from the standard library means writing each certificate to a
    # temp file for `ssl._ssl._test_decode_cert`, a private function, and the
    # only thing being asked here is whether the issuer name contains a word.
    # A subject CN is stored as plain bytes inside the DER, so a substring test
    # answers it - imprecisely, but this is corroborating evidence for a log
    # line, not the thing that decides whether the proxy is used.
    return ["Zscaler root certificate" for cert_bytes in windows_root_certificates()
            if b"Zscaler" in cert_bytes or b"zscaler" in cert_bytes]


def zscaler_processes():
    """Zscaler client processes currently running. Windows only."""
    if os.name != "nt":
        return []
    try:
        finished = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                  capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if finished.returncode != 0:
        return []
    running = finished.stdout.lower()
    return [name for name in ZSCALER_PROCESS_NAMES if name.lower() in running]


def system_proxy_setting():
    """The proxy Windows itself is configured to use, if any.

    Read from the WinINET settings, which is where a corporate policy usually
    lands. Reported as evidence; not used as the proxy, because the value there
    is often a PAC URL rather than a host pip can dial.
    """
    if os.name != "nt":
        return ""
    try:
        import winreg
    except ImportError:
        return ""
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
    except OSError:
        return ""
    try:
        with key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except FileNotFoundError:
                enabled = 0
            try:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except FileNotFoundError:
                server = ""
            try:
                auto_config, _ = winreg.QueryValueEx(key, "AutoConfigURL")
            except FileNotFoundError:
                auto_config = ""
    except OSError:
        return ""
    if enabled and server:
        return str(server)
    return str(auto_config or "")


def detect_zscaler(proxy_url=None):
    """What this machine says about Zscaler, as evidence rather than a verdict.

    Returns a dict. `proxy_reachable` is the one that decides whether the proxy
    gets used; the rest is for explaining the decision, which matters because
    "pip cannot reach PyPI" and "pip cannot reach PyPI because Zscaler is in
    the way" lead to very different next moves.
    """
    proxy_url = normalise_proxy(
        proxy_url or os.environ.get(ZSCALER_PROXY_ENV, "") or DEFAULT_ZSCALER_PROXY)
    host, port = split_host_port(proxy_url)

    report = {
        "proxy_url": proxy_url,
        "proxy_host": host,
        "proxy_port": port,
        "proxy_reachable": tcp_reachable(host, port),
        "certificates": zscaler_certificate_names(),
        "processes": zscaler_processes(),
        "system_proxy": system_proxy_setting(),
    }
    report["active"] = bool(
        report["proxy_reachable"] or report["certificates"] or report["processes"]
        or "zscaler" in report["system_proxy"].lower())
    return report


def describe_zscaler(report, log=print):
    """Print the evidence behind the proxy decision."""
    log("Network check:")
    reachable = "reachable" if report["proxy_reachable"] else "not reachable"
    log(f"  proxy {report['proxy_url']}: {reachable}")
    if report["certificates"]:
        log(f"  Zscaler root certificate installed: yes "
            f"({len(report['certificates'])} found)")
    if report["processes"]:
        log(f"  Zscaler client running: {', '.join(report['processes'])}")
    if report["system_proxy"]:
        log(f"  Windows proxy setting: {report['system_proxy']}")
    if not report["active"]:
        log("  no sign of Zscaler; going out directly")


# --- Getting pip through it --------------------------------------------------


def resolve_pip_proxy(report=None, log=print):
    """The proxy to hand pip, or "" for none.

    Order matters, and every step is a case that came up:

    1. PIPEINSERT_PIP_PROXY, which may be set to an empty string to force a
       direct connection on a machine where detection gets it wrong.
    2. A standard proxy variable already in the environment - the user has said
       how to get out, and overriding that would be rude.
    3. The Zscaler proxy, but only when it actually answers. Forcing it
       off-network would break a run that would otherwise have worked.
    """
    if PROXY_ENV in os.environ:
        chosen = normalise_proxy(os.environ[PROXY_ENV])
        log(f"Using the proxy from {PROXY_ENV}: {chosen or '(none)'}")
        return chosen

    for name in STANDARD_PROXY_ENV:
        if os.environ.get(name, "").strip():
            log(f"{name} is set, so pip's own proxy handling is left alone.")
            return ""

    report = report if report is not None else detect_zscaler()
    if report["proxy_reachable"]:
        log(f"Zscaler proxy detected. pip will go through {report['proxy_url']}")
        return report["proxy_url"]

    if report["active"]:
        log(f"Zscaler looks active on this machine, but {report['proxy_url']} "
            f"did not answer - so this is probably not the office network. "
            f"pip will try a direct connection. If it fails, set "
            f"{PROXY_ENV} to the right proxy.")
    return ""


def write_ca_bundle(venv_dir=VENV_DIR, log=print):
    """A PEM of the machine's trusted roots, for pip to verify against.

    Zscaler re-signs intercepted TLS with its own CA. That CA is in the Windows
    trust store, because IT put it there - which is why a browser is happy and
    pip is not: pip verifies against certifi's bundle, which contains public
    roots only.

    Returns the path, or None when there is nothing to write - which is every
    non-Windows machine, since `ssl.enum_certificates` is Windows-only.
    """
    override = os.environ.get(CA_BUNDLE_ENV, "").strip()
    if override:
        log(f"Using the CA bundle from {CA_BUNDLE_ENV}: {override}")
        return Path(override)

    certificates = windows_root_certificates()
    if not certificates:
        return None

    pem_parts = []
    for cert_bytes in certificates:
        try:
            pem_parts.append(ssl.DER_cert_to_PEM_cert(cert_bytes))
        except (ValueError, ssl.SSLError):
            # One unconvertible certificate must not cost the whole bundle.
            continue
    if not pem_parts:
        return None

    target = Path(venv_dir) / "corporate-ca-bundle.pem"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(pem_parts), encoding="ascii")
    except OSError as ex:
        log(f"Could not write a CA bundle to {target}: {ex}")
        return None

    log(f"Built a CA bundle from the Windows trust store: {target} "
        f"({len(pem_parts)} certificates)")
    return target


def pip_network_args(venv_dir=VENV_DIR, report=None, log=print):
    """The --proxy and --cert arguments this network needs, if any."""
    args = []
    proxy = resolve_pip_proxy(report, log=log)
    if proxy:
        args += ["--proxy", proxy]
        # Only worth building when something is intercepting. Off the corporate
        # network certifi is correct and the Windows store adds nothing.
        bundle = write_ca_bundle(venv_dir, log=log)
        if bundle:
            args += ["--cert", str(bundle)]
        elif os.name == "nt":
            log("Could not read the Windows trust store, so pip will verify "
                "against certifi. If it now fails with a certificate error, "
                f"Zscaler's root is the reason - point {CA_BUNDLE_ENV} at a PEM "
                f"copy of it.")
    return args


def pip_failure_hint(report, network_args):
    """What to try next, chosen from what the network looked like.

    A pip failure on a corporate network is nearly always one of three things,
    and the traceback names none of them.
    """
    used_proxy = "--proxy" in network_args
    used_cert = "--cert" in network_args

    if not used_proxy and report["active"]:
        return (f"Zscaler looks active but no proxy was used. Try:\n"
                f"    set {PROXY_ENV}={report['proxy_url']}")
    if not used_proxy:
        return (f"No proxy was used. If you are on the office network, try:\n"
                f"    set {PROXY_ENV}={DEFAULT_ZSCALER_PROXY}")
    if used_proxy and not used_cert:
        return (f"pip went through {report['proxy_url']} but verified against "
                f"certifi. If the error above mentions a certificate, point "
                f"{CA_BUNDLE_ENV} at a PEM copy of the Zscaler root.")
    return (f"pip went through {report['proxy_url']} and verified against the "
            f"Windows trust store. If the error above is still a certificate "
            f"problem, the Zscaler root may not be installed on this machine; "
            f"if it is a connection problem, the proxy address may have "
            f"changed - override it with {PROXY_ENV}.")


def write_pip_config(venv_dir=VENV_DIR, network_args=(), log=print):
    """Persist the proxy and certificate into the venv's own pip config.

    So that `\\.venv\\Scripts\\pip install something` later works the same way,
    without the user having to know any of this. pip reads pip.ini (Windows) or
    pip.conf from the root of the virtual environment it is running in.
    """
    settings = {}
    pairs = list(zip(network_args[::2], network_args[1::2]))
    for flag, value in pairs:
        settings[flag.lstrip("-")] = value
    if not settings:
        return None

    target = Path(venv_dir) / ("pip.ini" if os.name == "nt" else "pip.conf")
    lines = ["[global]"] + [f"{key} = {value}" for key, value in settings.items()]
    try:
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as ex:
        log(f"Could not write {target}: {ex}")
        return None
    log(f"Wrote {target}, so pip in this environment keeps these settings.")
    return target


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

    report = detect_zscaler()
    describe_zscaler(report, log=log)
    network = pip_network_args(venv_dir, report, log=log)
    if network:
        write_pip_config(venv_dir, network, log=log)

    if upgrade_pip:
        log("Upgrading pip in the new environment")
        subprocess.run([str(python_exe), "-m", "pip", "install", "--upgrade",
                        "pip", "--quiet", *network])

    log(f"Installing {REQUIREMENTS.name}. A first run downloads the geospatial "
        f"stack and takes a few minutes.")
    finished = subprocess.run([str(python_exe), "-m", "pip", "install",
                               "-r", str(REQUIREMENTS), *network])
    if finished.returncode != 0:
        raise RuntimeError(
            f"pip exited {finished.returncode} installing {REQUIREMENTS}.\n"
            f"{pip_failure_hint(report, network)}\n"
            f"To install by hand:\n"
            f"    {python_exe} -m pip install -r {REQUIREMENTS} "
            f"{' '.join(network)}")

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

    if "--network" in argv:
        report = detect_zscaler()
        print(f"Interpreter: {sys.executable}")
        describe_zscaler(report)
        proxy = resolve_pip_proxy(report)
        print(f"\npip would use: {proxy or 'a direct connection'}")
        if proxy and os.name == "nt":
            print("and verify against the Windows trust store, so Zscaler's "
                  "re-signed certificates are accepted.")
        return 0 if (proxy or not report["active"]) else 1

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
