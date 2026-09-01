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

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the script needs bash"
)


@pytest.fixture(scope="module")
def script(pytestconfig):
    path = pytestconfig.rootpath / "run.sh"
    assert path.is_file(), "run.sh is missing"
    return path


def run(script, *arguments) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *arguments],
        capture_output=True,
        text=True,
        cwd=script.parent,
        timeout=60,
    )


class TestTheScriptItself:
    def test_is_executable(self, script):
        assert script.stat().st_mode & stat.S_IXUSR, "run.sh should be executable"

    def test_is_valid_bash(self, script):
        check = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=60
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


class TestDriveImportIsOptional:
    """The launcher must work without Google configuration, and say so."""

    def test_it_documents_the_optional_feature(self, script):
        printed = run(script, "--help").stdout
        assert ".env" in printed
        assert "Google Drive" in printed

    def test_the_help_says_it_performs_no_authorization(self, script):
        assert "authorization" in run(script, "--help").stdout

    def test_it_reads_a_dot_env_file_rather_than_requiring_one(self, script):
        text = script.read_text()
        assert 'if [[ -f "$ENV_FILE" ]]; then' in text, (
            "a missing .env must be a normal state, not a failure"
        )

    def test_it_reports_whether_drive_import_is_enabled(self, script):
        assert "drive import" in script.read_text()

    def test_it_holds_no_credentials(self, script):
        text = script.read_text()
        for marker in ("apps.googleusercontent.com", "AIza", "client_secret"):
            assert marker not in text

    def test_it_never_echoes_a_configured_value(self, script):
        """The status line reports enabled or disabled, never what was set."""
        text = script.read_text()
        for variable in ("HEN_DRIVE_CLIENT_ID", "HEN_DRIVE_API_KEY", "HEN_DRIVE_APP_ID"):
            assert f"${{{variable}}}" not in text
            assert f"${variable}" not in text

    def test_it_quotes_every_path_it_expands(self, script):
        """The repository path can hold spaces and non-ASCII characters."""
        text = script.read_text()
        for unquoted in ("cd $ROOT", "source $ENV_FILE", "-f $ENV_FILE"):
            assert unquoted not in text
