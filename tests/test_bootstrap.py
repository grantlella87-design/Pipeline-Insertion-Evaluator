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
