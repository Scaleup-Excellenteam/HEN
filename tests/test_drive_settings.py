"""Tests for the Drive feature's environment-based settings.

The behaviour that matters most here is the default: with nothing set the
feature must be off, and being off must need no Google configuration at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autocomplete.config import Config
from autocomplete.drive.settings import (
    DriveSettings,
    DriveSettingsError,
    load_settings,
)

COMPLETE = {
    "HEN_DRIVE_ENABLED": "true",
    "HEN_DRIVE_CLIENT_ID": "client",
    "HEN_DRIVE_API_KEY": "key",
    "HEN_DRIVE_APP_ID": "123",
}


class TestDefaults:
    def test_an_empty_environment_disables_the_feature(self):
        settings = DriveSettings.from_environment({})
        assert settings.enabled is False
        assert settings.configured is False
        assert settings.missing == ()

    def test_disabled_needs_no_google_identifiers(self):
        settings = DriveSettings.from_environment({})
        assert settings.client_id == ""
        assert settings.api_key == ""
        assert settings.app_id == ""

    def test_unrelated_variables_are_ignored(self):
        settings = DriveSettings.from_environment(
            {"PATH": "/usr/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/key.json"}
        )
        assert settings.enabled is False

    def test_the_limits_have_documented_defaults(self):
        settings = DriveSettings.from_environment({})
        assert settings.max_files == 10
        assert settings.max_file_bytes == 10 * 1024 * 1024
        assert settings.max_total_bytes == 50 * 1024 * 1024
        assert settings.supported_mime_types == (
            "text/plain",
            "application/vnd.google-apps.document",
        )


class TestEnabling:
    def test_enabled_and_complete_is_configured(self):
        assert DriveSettings.from_environment(COMPLETE).configured is True

    @pytest.mark.parametrize(
        "dropped",
        ["HEN_DRIVE_CLIENT_ID", "HEN_DRIVE_API_KEY", "HEN_DRIVE_APP_ID"],
    )
    def test_enabled_but_incomplete_reports_what_is_missing(self, dropped):
        environment = {key: value for key, value in COMPLETE.items() if key != dropped}
        settings = DriveSettings.from_environment(environment)
        assert settings.enabled is True
        assert settings.configured is False
        assert settings.missing == (dropped,)
        assert dropped in settings.describe_missing()

    def test_incomplete_settings_do_not_raise(self):
        """A half-finished deployment degrades to off, it does not take the
        server down."""
        DriveSettings.from_environment({"HEN_DRIVE_ENABLED": "true"})

    def test_missing_is_empty_while_disabled_even_without_identifiers(self):
        assert DriveSettings.from_environment({"HEN_DRIVE_ENABLED": "false"}).missing == ()

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_spellings(self, value):
        assert DriveSettings.from_environment({"HEN_DRIVE_ENABLED": value}).enabled

    @pytest.mark.parametrize("value", ["0", "false", "No", "off", ""])
    def test_falsy_spellings(self, value):
        assert not DriveSettings.from_environment({"HEN_DRIVE_ENABLED": value}).enabled

    def test_an_unreadable_switch_is_rejected(self):
        with pytest.raises(DriveSettingsError, match="true or false"):
            DriveSettings.from_environment({"HEN_DRIVE_ENABLED": "perhaps"})

    def test_whitespace_around_a_value_is_ignored(self):
        settings = DriveSettings.from_environment({**COMPLETE, "HEN_DRIVE_CLIENT_ID": "  c  "})
        assert settings.client_id == "c"


class TestLimits:
    def test_limits_can_be_lowered(self):
        settings = DriveSettings.from_environment(
            {"HEN_DRIVE_MAX_FILES": "2", "HEN_DRIVE_MAX_FILE_BYTES": "1024",
             "HEN_DRIVE_MAX_TOTAL_BYTES": "4096"}
        )
        assert (settings.max_files, settings.max_file_bytes, settings.max_total_bytes) == (
            2,
            1024,
            4096,
        )

    @pytest.mark.parametrize("name", ["MAX_FILES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES"])
    def test_a_limit_of_zero_is_rejected(self, name):
        with pytest.raises(DriveSettingsError, match="at least 1"):
            DriveSettings.from_environment({f"HEN_DRIVE_{name}": "0"})

    def test_a_limit_that_is_not_a_number_is_rejected(self):
        with pytest.raises(DriveSettingsError, match="whole number"):
            DriveSettings.from_environment({"HEN_DRIVE_MAX_FILES": "lots"})

    def test_a_negative_limit_is_rejected(self):
        with pytest.raises(DriveSettingsError, match="not be negative"):
            DriveSettings.from_environment({"HEN_DRIVE_MAX_FILES": "-1"})

    def test_a_total_below_the_per_file_limit_is_rejected(self):
        with pytest.raises(DriveSettingsError, match="no file could be imported"):
            DriveSettings.from_environment(
                {"HEN_DRIVE_MAX_FILE_BYTES": "1000", "HEN_DRIVE_MAX_TOTAL_BYTES": "10"}
            )

    def test_a_timeout_must_be_positive(self):
        with pytest.raises(DriveSettingsError, match="positive"):
            DriveSettings.from_environment({"HEN_DRIVE_HTTP_TIMEOUT_SECONDS": "0"})

    def test_retries_may_be_zero(self):
        assert DriveSettings.from_environment({"HEN_DRIVE_HTTP_RETRIES": "0"}).retries == 0


class TestDataDirectory:
    def test_a_relative_directory_is_anchored_to_the_base(self, tmp_path):
        settings = DriveSettings.from_environment(
            {"HEN_DRIVE_DATA_DIR": "imported"}, base_dir=tmp_path
        )
        assert settings.data_dir == tmp_path / "imported"

    def test_an_absolute_directory_is_left_alone(self, tmp_path):
        target = tmp_path / "elsewhere"
        settings = DriveSettings.from_environment(
            {"HEN_DRIVE_DATA_DIR": str(target)}, base_dir=tmp_path / "other"
        )
        assert settings.data_dir == target

    def test_the_default_directory_is_anchored_too(self, tmp_path):
        settings = DriveSettings.from_environment({}, base_dir=tmp_path)
        assert settings.data_dir == tmp_path / ".drive-data"

    def test_load_settings_anchors_beside_the_cache(self, tmp_path):
        config = Config(cache_dir=tmp_path / "project" / ".cache")
        settings = load_settings(config, {})
        assert settings.data_dir == tmp_path / "project" / ".drive-data"

    def test_load_settings_without_a_config_leaves_it_relative(self):
        assert load_settings(None, {}).data_dir == Path(".drive-data")
