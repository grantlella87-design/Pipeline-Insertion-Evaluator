"""Tests for the venv bootstrap.

None of these create a virtual environment or run pip - that takes minutes and
needs a network. They cover the decisions instead: which interpreter is
running, what is missing, and whether a re-exec would loop. The install itself
is the part that is obvious when it breaks; these are the parts that fail
quietly.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import bootstrap


class TestStdlibOnly:
    def test_bootstrap_imports_with_no_third_party_packages(self):
        """The whole point: it runs where the dependencies are missing.

        Run in a subprocess with an import hook that rejects every third-party
        package the project needs, so a stray `import geopandas` added to this
        module - or to anything it imports - fails here rather than on a user's
        fresh checkout.
        """
        guard = (
            "import sys\n"
            "BANNED = %r\n"
            "class Blocker:\n"
            "    def find_module(self, name, path=None):\n"
            "        if name.split('.')[0] in BANNED:\n"
            "            raise ImportError('blocked: ' + name)\n"
            "        return None\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in BANNED:\n"
            "            raise ImportError('blocked: ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "sys.path.insert(0, %r)\n"
            "import bootstrap\n"
            "print('ok')\n"
        ) % (list(bootstrap.REQUIRED_IMPORTS), str(REPO_ROOT))

        finished = subprocess.run([sys.executable, "-c", guard],
                                  capture_output=True, text=True, timeout=60)
        assert finished.returncode == 0, finished.stderr
        assert "ok" in finished.stdout


class TestVenvPython:
    def test_platform_layout(self):
        # Windows puts it in Scripts\ and everything else in bin/.
        path = bootstrap.venv_python(Path("/project/.venv"))
        if os.name == "nt":
            assert path.parts[-2:] == ("Scripts", "python.exe")
        else:
            assert path.parts[-2:] == ("bin", "python")

    def test_is_under_the_given_directory(self):
        venv = Path("/project/.venv")
        assert venv in bootstrap.venv_python(venv).parents


class TestRunningIn:
    def test_false_for_a_directory_that_is_not_this_environment(self, tmp_path):
        assert bootstrap.running_in(tmp_path / "nowhere") is False

    def test_true_for_this_interpreters_own_prefix(self):
        # sys.prefix is what a venv points at itself, so this holds whether or
        # not the tests are being run inside one.
        assert bootstrap.running_in(Path(sys.prefix)) is True

    def test_compares_the_prefix_not_the_interpreter_path(self):
        """A venv's bin/python is a symlink to the base interpreter.

        Comparing resolved interpreter paths made every Python sharing that
        base look like it was already inside the venv, which sent a plain
        `python run.py` down the install-in-place branch and ran pip on every
        invocation.
        """
        base_prefix = Path(sys.base_prefix)
        if base_prefix.resolve() == Path(sys.prefix).resolve():
            pytest.skip("not running inside a venv, so there is no pair to compare")
        assert bootstrap.running_in(base_prefix) is False


class TestMissingImports:
    def test_a_present_module_is_not_missing(self):
        assert bootstrap.missing_imports(("json", "os")) == []

    def test_an_absent_module_is_missing(self):
        assert bootstrap.missing_imports(
            ("definitely_not_installed_xyz",)) == ["definitely_not_installed_xyz"]

    def test_reports_only_what_is_absent(self):
        assert bootstrap.missing_imports(
            ("json", "definitely_not_installed_xyz")) == [
                "definitely_not_installed_xyz"]

    def test_the_required_list_matches_what_the_code_imports(self):
        """Every third-party top-level import in src/ must be covered.

        A package added to an import line but not here is one the bootstrap
        will not install, and the run fails on a fresh machine exactly the way
        this was written to prevent.
        """
        import ast

        stdlib = set(sys.stdlib_module_names)
        local = {"pipelineinsertion", "pipeline_insertion_evaluator",
                 "leaflet_bbox_server", "bootstrap", "_bootstrap"}
        allowed = (set(bootstrap.REQUIRED_IMPORTS)
                   | set(bootstrap.OPTIONAL_IMPORTS) | stdlib | local)
        # Engines geopandas pulls in. The map server asks for whichever of them
        # exists, so neither is named in requirements.txt on its own.
        allowed |= {"fiona", "pyogrio"}

        offenders = {}
        for path in (REPO_ROOT / "src").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for name in names:
                    if name and name not in allowed:
                        offenders.setdefault(name, []).append(path.name)

        assert not offenders, (
            f"imported in src/ but not in bootstrap.REQUIRED_IMPORTS: "
            f"{ {k: sorted(set(v)) for k, v in offenders.items()} }")


class TestMissingImportsIn:
    def test_probes_another_interpreter(self):
        # This interpreter, asked from the outside, must agree with itself.
        assert bootstrap.missing_imports_in(sys.executable, ("json",)) == []
        assert bootstrap.missing_imports_in(
            sys.executable, ("definitely_not_installed_xyz",)) == [
                "definitely_not_installed_xyz"]

    def test_an_interpreter_that_cannot_run_satisfies_nothing(self, tmp_path):
        absent = tmp_path / "no-such-python"
        assert bootstrap.missing_imports_in(absent, ("json",)) == ["json"]


class TestEnsure:
    def test_does_nothing_when_the_interpreter_is_already_usable(self, monkeypatch):
        monkeypatch.setattr(bootstrap, "missing_imports", lambda *a, **k: [])
        called = []
        monkeypatch.setattr(bootstrap, "create_venv",
                            lambda *a, **k: called.append("create"))
        assert bootstrap.ensure(["run.py"]) is None
        assert called == []

    def test_opt_out_short_circuits(self, monkeypatch):
        # A machine that manages its own environment - conda, a container -
        # must be able to say so and be left alone.
        monkeypatch.setenv(bootstrap.OPT_OUT_ENV, "1")
        monkeypatch.setattr(bootstrap, "missing_imports", lambda *a, **k: ["geopandas"])
        called = []
        monkeypatch.setattr(bootstrap, "create_venv",
                            lambda *a, **k: called.append("create"))
        assert bootstrap.ensure(["run.py"]) is None
        assert called == []

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_opt_out_accepts_the_usual_spellings(self, monkeypatch, value):
        monkeypatch.setenv(bootstrap.OPT_OUT_ENV, value)
        monkeypatch.setattr(bootstrap, "missing_imports", lambda *a, **k: ["geopandas"])
        assert bootstrap.ensure(["run.py"]) is None

    def test_a_second_failure_raises_instead_of_looping(self, monkeypatch):
        """The re-exec must not be able to recurse.

        The sentinel is set before handing off, so an environment that still
        cannot import what it needs reports that once rather than spawning
        itself forever.
        """
        monkeypatch.setenv(bootstrap.SENTINEL_ENV, "1")
        monkeypatch.setattr(bootstrap, "missing_imports", lambda *a, **k: ["geopandas"])
        with pytest.raises(RuntimeError, match="still missing"):
            bootstrap.ensure(["run.py"])

    def test_the_sentinel_is_set_for_the_child(self, monkeypatch, tmp_path):
        captured = {}

        class Result:
            returncode = 0

        def fake_run(command, env=None, **kwargs):
            captured["env"] = env
            captured["command"] = command
            return Result()

        monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
        bootstrap.reexec(["run.py", "--flag"], tmp_path, log=lambda *a: None)

        assert captured["env"][bootstrap.SENTINEL_ENV] == "1"
        assert captured["command"][1:] == ["run.py", "--flag"]
        assert str(bootstrap.venv_python(tmp_path)) == captured["command"][0]

    def test_arguments_are_passed_through(self, monkeypatch, tmp_path):
        captured = {}

        class Result:
            returncode = 7

        monkeypatch.setattr(bootstrap.subprocess, "run",
                            lambda command, **kw: (captured.update(command=command),
                                                   Result())[1])
        code = bootstrap.reexec(["run.py", "--no-view", "--port", "8800"],
                                tmp_path, log=lambda *a: None)
        assert code == 7
        assert captured["command"][1:] == ["run.py", "--no-view", "--port", "8800"]

    def test_ctrl_c_during_the_child_is_not_a_traceback(self, monkeypatch, tmp_path):
        # Both processes are in the same console group, so Ctrl+C at the map
        # server reaches this parent too.
        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(bootstrap.subprocess, "run", interrupt)
        assert bootstrap.reexec(["run.py"], tmp_path, log=lambda *a: None) == 130


class TestCreateVenv:
    def test_an_existing_venv_is_not_recreated(self, monkeypatch, tmp_path):
        python_path = bootstrap.venv_python(tmp_path)
        python_path.parent.mkdir(parents=True)
        python_path.write_text("")

        called = []
        monkeypatch.setattr(bootstrap.subprocess, "run",
                            lambda *a, **k: called.append(a))
        assert bootstrap.create_venv(tmp_path, log=lambda *a: None) is False
        assert called == []

    def test_a_failed_creation_says_what_to_do(self, monkeypatch, tmp_path):
        class Result:
            returncode = 1

        monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: Result())
        with pytest.raises(RuntimeError, match="python3-venv|Could not create"):
            bootstrap.create_venv(tmp_path / "new", log=lambda *a: None)


class TestRequirementsFile:
    def test_it_lists_every_required_import(self):
        text = bootstrap.REQUIREMENTS.read_text(encoding="utf-8-sig").lower()
        for name in bootstrap.REQUIRED_IMPORTS:
            assert name in text, f"{name} is not in requirements.txt"

    def test_it_lists_the_optional_imports_too(self):
        """Optional to import, not optional to install.

        truststore is imported inside a try/except so a machine without it
        still runs - but it is what puts the OS certificate store into TLS, so
        a venv built without it can fail every request on a corporate network
        that the system Python handled fine.
        """
        text = bootstrap.REQUIREMENTS.read_text(encoding="utf-8-sig").lower()
        for name in bootstrap.OPTIONAL_IMPORTS:
            assert name in text, f"{name} is not in requirements.txt"

    def test_pyproject_and_requirements_agree(self):
        # Two lists of the same dependencies drift; this is the check that they
        # have not.
        pyproject = (bootstrap.REPO_ROOT / "pyproject.toml").read_text(
            encoding="utf-8-sig").lower()
        for name in bootstrap.REQUIRED_IMPORTS + bootstrap.OPTIONAL_IMPORTS:
            assert name in pyproject, f"{name} is not in pyproject.toml"


class TestProxyUrlParsing:
    @pytest.mark.parametrize("url,expected", [
        ("http://zscaler.nationalgrid.com:80", ("zscaler.nationalgrid.com", 80)),
        ("zscaler.nationalgrid.com", ("zscaler.nationalgrid.com", 80)),
        ("http://proxy:3128", ("proxy", 3128)),
        ("http://user:pw@proxy:3128", ("proxy", 3128)),
        ("http://proxy", ("proxy", 80)),
    ])
    def test_split_host_port(self, url, expected):
        assert bootstrap.split_host_port(url) == expected

    def test_a_nonsense_port_falls_back_rather_than_raising(self):
        host, port = bootstrap.split_host_port("http://proxy:notaport")
        assert port == 80

    @pytest.mark.parametrize("url,expected", [
        ("zscaler.nationalgrid.com:80", "http://zscaler.nationalgrid.com:80"),
        ("http://proxy:3128", "http://proxy:3128"),
        ("https://proxy:3128", "https://proxy:3128"),
        ("  proxy  ", "http://proxy"),
        ("", ""),
    ])
    def test_normalise_proxy_adds_a_scheme(self, url, expected):
        assert bootstrap.normalise_proxy(url) == expected

    def test_the_default_is_the_nationalgrid_zscaler(self):
        assert bootstrap.DEFAULT_ZSCALER_PROXY == "http://zscaler.nationalgrid.com:80"


class TestTcpReachable:
    def test_a_listening_socket_is_reachable(self):
        import socket

        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            assert bootstrap.tcp_reachable(*server.getsockname(), timeout=3) is True
        finally:
            server.close()

    def test_a_closed_port_is_not(self):
        # Port 9 (discard) is reserved and not listening on a normal machine.
        assert bootstrap.tcp_reachable("127.0.0.1", 9, timeout=1) is False

    def test_an_unresolvable_host_is_not(self):
        assert bootstrap.tcp_reachable(
            "no-such-host.invalid", 80, timeout=3) is False

    def test_a_nonsense_port_does_not_raise(self):
        assert bootstrap.tcp_reachable("127.0.0.1", "abc", timeout=1) is False


@pytest.fixture
def no_proxy_env(monkeypatch):
    """Clear every proxy variable, so detection rather than the host is tested."""
    for name in bootstrap.STANDARD_PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(bootstrap.PROXY_ENV, raising=False)
    monkeypatch.delenv(bootstrap.ZSCALER_PROXY_ENV, raising=False)
    monkeypatch.delenv(bootstrap.CA_BUNDLE_ENV, raising=False)


@pytest.fixture
def listening_proxy():
    """A socket standing in for the Zscaler proxy."""
    import socket
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)

    def accept_forever():
        while True:
            try:
                server.accept()
            except OSError:
                return

    threading.Thread(target=accept_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}"
    server.close()


class TestDetectZscaler:
    def test_a_reachable_proxy_counts_as_active(self, no_proxy_env, monkeypatch,
                                                listening_proxy):
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        report = bootstrap.detect_zscaler()
        assert report["proxy_reachable"] is True
        assert report["active"] is True

    def test_an_unreachable_proxy_with_no_other_evidence_is_not_active(
            self, no_proxy_env, monkeypatch):
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, "http://127.0.0.1:9")
        monkeypatch.setattr(bootstrap, "zscaler_certificate_names", lambda: [])
        monkeypatch.setattr(bootstrap, "zscaler_processes", lambda: [])
        monkeypatch.setattr(bootstrap, "system_proxy_setting", lambda: "")
        report = bootstrap.detect_zscaler()
        assert report["proxy_reachable"] is False
        assert report["active"] is False

    def test_an_installed_certificate_alone_counts_as_active(
            self, no_proxy_env, monkeypatch):
        """Zscaler tunnelling from home, where the office proxy is unreachable.

        Worth distinguishing from "no Zscaler at all", because the advice
        differs: one needs a proxy address, the other needs nothing.
        """
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, "http://127.0.0.1:9")
        monkeypatch.setattr(bootstrap, "zscaler_certificate_names",
                            lambda: ["Zscaler root certificate"])
        monkeypatch.setattr(bootstrap, "zscaler_processes", lambda: [])
        monkeypatch.setattr(bootstrap, "system_proxy_setting", lambda: "")
        report = bootstrap.detect_zscaler()
        assert report["active"] is True
        assert report["proxy_reachable"] is False

    def test_the_report_carries_the_url_it_probed(self, no_proxy_env):
        report = bootstrap.detect_zscaler("http://example.invalid:8080")
        assert report["proxy_url"] == "http://example.invalid:8080"
        assert report["proxy_host"] == "example.invalid"
        assert report["proxy_port"] == 8080

    def test_detection_is_cheap_off_network(self, no_proxy_env, monkeypatch):
        # Someone at home must not wait on this. The probe timeout bounds it.
        assert bootstrap.PROXY_PROBE_TIMEOUT_SECONDS <= 5


class TestResolvePipProxy:
    def test_a_reachable_zscaler_is_used(self, no_proxy_env, monkeypatch,
                                         listening_proxy):
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        report = bootstrap.detect_zscaler()
        assert bootstrap.resolve_pip_proxy(report, log=lambda *a: None) == listening_proxy

    def test_an_unreachable_zscaler_is_not(self, no_proxy_env, monkeypatch):
        """Off the corporate network, forcing the proxy breaks a working run."""
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, "http://127.0.0.1:9")
        report = bootstrap.detect_zscaler()
        assert bootstrap.resolve_pip_proxy(report, log=lambda *a: None) == ""

    def test_explicit_override_wins_over_detection(self, no_proxy_env, monkeypatch,
                                                   listening_proxy):
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        monkeypatch.setenv(bootstrap.PROXY_ENV, "http://chosen:3128")
        report = bootstrap.detect_zscaler()
        assert bootstrap.resolve_pip_proxy(
            report, log=lambda *a: None) == "http://chosen:3128"

    def test_an_empty_override_forces_a_direct_connection(
            self, no_proxy_env, monkeypatch, listening_proxy):
        # The escape hatch for a machine where detection gets it wrong.
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        monkeypatch.setenv(bootstrap.PROXY_ENV, "")
        report = bootstrap.detect_zscaler()
        assert bootstrap.resolve_pip_proxy(report, log=lambda *a: None) == ""

    def test_an_override_without_a_scheme_is_normalised(self, no_proxy_env,
                                                        monkeypatch):
        monkeypatch.setenv(bootstrap.PROXY_ENV, "zscaler.nationalgrid.com:80")
        assert bootstrap.resolve_pip_proxy(None, log=lambda *a: None) == (
            "http://zscaler.nationalgrid.com:80")

    @pytest.mark.parametrize("name", ["HTTPS_PROXY", "http_proxy", "ALL_PROXY"])
    def test_an_existing_proxy_variable_is_left_alone(self, no_proxy_env,
                                                      monkeypatch, name,
                                                      listening_proxy):
        """pip reads these itself; overriding would ignore a deliberate choice."""
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        monkeypatch.setenv(name, "http://already:8080")
        report = bootstrap.detect_zscaler()
        assert bootstrap.resolve_pip_proxy(report, log=lambda *a: None) == ""


class TestPipNetworkArgs:
    def test_no_proxy_means_no_arguments(self, no_proxy_env, monkeypatch, tmp_path):
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, "http://127.0.0.1:9")
        assert bootstrap.pip_network_args(tmp_path, log=lambda *a: None) == []

    def test_a_detected_proxy_becomes_a_pip_argument(self, no_proxy_env, monkeypatch,
                                                     tmp_path, listening_proxy):
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        args = bootstrap.pip_network_args(tmp_path, log=lambda *a: None)
        assert args[:2] == ["--proxy", listening_proxy]

    def test_a_certificate_bundle_is_added_when_one_can_be_built(
            self, no_proxy_env, monkeypatch, tmp_path, listening_proxy):
        """The half that is easy to forget.

        A working proxy turns the connection reset into an SSL error, because
        Zscaler re-signs with a CA certifi does not carry.
        """
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        monkeypatch.setattr(bootstrap, "write_ca_bundle",
                            lambda *a, **k: tmp_path / "ca.pem")
        args = bootstrap.pip_network_args(tmp_path, log=lambda *a: None)
        assert "--cert" in args
        assert str(tmp_path / "ca.pem") in args

    def test_arguments_come_in_flag_value_pairs(self, no_proxy_env, monkeypatch,
                                                tmp_path, listening_proxy):
        # write_pip_config pairs them off, so an odd-length list would silently
        # write a wrong config file.
        monkeypatch.setenv(bootstrap.ZSCALER_PROXY_ENV, listening_proxy)
        monkeypatch.setattr(bootstrap, "write_ca_bundle",
                            lambda *a, **k: tmp_path / "ca.pem")
        args = bootstrap.pip_network_args(tmp_path, log=lambda *a: None)
        assert len(args) % 2 == 0
        assert all(flag.startswith("--") for flag in args[::2])


class TestCaBundle:
    def test_an_override_is_honoured(self, no_proxy_env, monkeypatch, tmp_path):
        monkeypatch.setenv(bootstrap.CA_BUNDLE_ENV, str(tmp_path / "mine.pem"))
        assert bootstrap.write_ca_bundle(tmp_path, log=lambda *a: None) == (
            tmp_path / "mine.pem")

    def test_nothing_is_written_where_there_is_no_windows_store(
            self, no_proxy_env, monkeypatch, tmp_path):
        monkeypatch.setattr(bootstrap, "windows_root_certificates", lambda: [])
        assert bootstrap.write_ca_bundle(tmp_path, log=lambda *a: None) is None

    def test_certificates_are_written_as_pem(self, no_proxy_env, monkeypatch,
                                             tmp_path):
        # DER_cert_to_PEM_cert base64-encodes and wraps; it does not parse, so
        # the content of the blob is irrelevant to what is being checked here -
        # that every certificate found reaches the file in the form pip's
        # --cert expects.
        monkeypatch.setattr(bootstrap, "windows_root_certificates",
                            lambda: [b"first-cert-bytes", b"second-cert-bytes"])
        written = bootstrap.write_ca_bundle(tmp_path, log=lambda *a: None)

        assert written is not None
        text = written.read_text()
        assert text.count("-----BEGIN CERTIFICATE-----") == 2
        assert text.count("-----END CERTIFICATE-----") == 2

    def test_the_bundle_lands_inside_the_venv(self, no_proxy_env, monkeypatch,
                                              tmp_path):
        # Which is gitignored, so a machine-specific trust bundle is never
        # committed.
        monkeypatch.setattr(bootstrap, "windows_root_certificates",
                            lambda: [b"cert"])
        written = bootstrap.write_ca_bundle(tmp_path, log=lambda *a: None)
        assert written.parent == tmp_path

    def test_an_unconvertible_certificate_does_not_cost_the_bundle(
            self, no_proxy_env, monkeypatch, tmp_path):
        # Captured before patching: bootstrap.ssl is the ssl module itself, so
        # calling through the module inside the replacement would recurse into
        # the replacement.
        original = bootstrap.ssl.DER_cert_to_PEM_cert

        def convert(cert_bytes):
            if cert_bytes == b"bad":
                raise ValueError("not a certificate")
            return original(cert_bytes)

        monkeypatch.setattr(bootstrap, "windows_root_certificates",
                            lambda: [b"good", b"bad", b"alsogood"])
        monkeypatch.setattr(bootstrap.ssl, "DER_cert_to_PEM_cert", convert)
        written = bootstrap.write_ca_bundle(tmp_path, log=lambda *a: None)
        assert written.read_text().count("-----BEGIN CERTIFICATE-----") == 2


class TestWritePipConfig:
    def test_settings_are_persisted_for_later_manual_pip_use(self, tmp_path):
        written = bootstrap.write_pip_config(
            tmp_path, ["--proxy", "http://p:80", "--cert", "C:\\ca.pem"],
            log=lambda *a: None)
        text = written.read_text()
        assert "[global]" in text
        assert "proxy = http://p:80" in text
        assert "cert = C:\\ca.pem" in text

    def test_the_filename_matches_what_pip_looks_for(self, tmp_path):
        written = bootstrap.write_pip_config(
            tmp_path, ["--proxy", "http://p:80"], log=lambda *a: None)
        assert written.name == ("pip.ini" if os.name == "nt" else "pip.conf")

    def test_nothing_is_written_when_there_is_nothing_to_say(self, tmp_path):
        assert bootstrap.write_pip_config(tmp_path, [], log=lambda *a: None) is None


class TestPipFailureHint:
    def test_names_the_proxy_when_none_was_used_but_zscaler_is_active(self):
        report = {"active": True, "proxy_url": "http://z:80"}
        assert bootstrap.PROXY_ENV in bootstrap.pip_failure_hint(report, [])

    def test_names_the_certificate_when_a_proxy_was_used_without_one(self):
        report = {"active": True, "proxy_url": "http://z:80"}
        hint = bootstrap.pip_failure_hint(report, ["--proxy", "http://z:80"])
        assert bootstrap.CA_BUNDLE_ENV in hint

    def test_suggests_the_default_proxy_off_network(self):
        report = {"active": False, "proxy_url": "http://z:80"}
        assert bootstrap.DEFAULT_ZSCALER_PROXY in bootstrap.pip_failure_hint(report, [])
