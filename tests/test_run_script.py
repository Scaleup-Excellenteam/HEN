"""Tests for the build-and-run script.

Only the parts that have no side effects: the script's own validity, and how it
answers before it touches anything. Starting or stopping servers is left out on
purpose, since a test run must not shut down a server the developer is using.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess

import pytest

def usable_bash() -> str | None:
    """The bash that would actually run the script, or ``None`` if there is none.

    Finding ``bash`` on PATH is not the same as having one. Windows ships a
    ``bash.exe`` in System32 that only launches WSL, and on a machine with no
    distribution installed it explains that instead of running anything, so
    the name resolves while nothing behind it works. Probing it is the only
    way to tell a usable shell from a placeholder, and a machine with Git for
    Windows earlier on PATH does have a usable one, which is worth running
    these against rather than skipping wholesale.
    """
    found = shutil.which("bash")
    if found is None:
        return None
    try:
        probe = subprocess.run([found, "-c", "exit 0"], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.returncode == 0 else None


BASH = usable_bash()

pytestmark = pytest.mark.skipif(BASH is None, reason="the script needs a working bash")


@pytest.fixture(scope="module")
def script(pytestconfig):
    path = pytestconfig.rootpath / "run.sh"
    assert path.is_file(), "run.sh is missing"
    return path


def run(script, *arguments) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(script), *arguments],
        capture_output=True,
        text=True,
        cwd=script.parent,
        timeout=60,
    )


class TestTheScriptItself:
    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows has no POSIX permission bits for a checkout to carry",
    )
    def test_is_executable(self, script):
        assert script.stat().st_mode & stat.S_IXUSR, "run.sh should be executable"

    def test_is_valid_bash(self, script):
        check = subprocess.run(
            [BASH, "-n", str(script)], capture_output=True, text=True, timeout=60
        )
        assert check.returncode == 0, check.stderr

    def test_stops_on_the_first_failure(self, script):
        """Without this a failed build would be followed by starting anyway."""
        assert "set -euo pipefail" in script.read_text()

    def test_holds_no_machine_specific_paths(self, script):
        text = script.read_text()
        assert os.path.expanduser("~") not in text
        assert "/Users/" not in text


class TestHelp:
    def test_help_succeeds(self, script):
        assert run(script, "--help").returncode == 0

    def test_help_lists_every_option_the_script_accepts(self, script):
        printed = run(script, "--help").stdout
        for option in ("--dev", "--stop", "--rebuild-index", "--skip-install"):
            assert option in printed, f"{option} is undocumented"

    def test_help_says_what_the_script_does_about_a_running_project(self, script):
        assert "stopped first" in run(script, "--help").stdout

    def test_help_points_at_the_command_line_as_well(self, script):
        assert "main.py" in run(script, "--help").stdout

    def test_help_does_not_leak_the_script_body(self, script):
        assert "set -euo" not in run(script, "--help").stdout


class TestArguments:
    def test_an_unknown_option_fails_clearly(self, script):
        result = run(script, "--wat")
        assert result.returncode != 0
        assert "unknown option" in result.stderr

    def test_an_unknown_option_stops_before_doing_anything(self, script):
        """Rejection happens while reading arguments, so nothing is stopped,
        installed or built on the way to the error."""
        result = run(script, "--wat")
        assert "Stopping" not in result.stdout
        assert "Installing" not in result.stdout
