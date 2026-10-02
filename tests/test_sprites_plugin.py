"""Unit tests for the sprites plugin: tagging, provisioning, and config threading.

These tests mock the sprites SDK and hermes-agent internals so they run
without a SPRITES_TOKEN or a live sprite.  Run with:

    nix develop -c pytest tests/test_sprites_plugin.py
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project root is on sys.path for direct module imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ------------------------------------------------------------------ #
#  _resolve_labels                                                    #
# ------------------------------------------------------------------ #

class TestResolveLabels:
    """Label resolution: static tags, auto_tags, sanitization, dedup."""

    def test_static_labels(self):
        from sprites_environment import _resolve_labels
        assert _resolve_labels(["prod", "web"], False, "task1") == ["prod", "web"]

    def test_none_labels(self):
        from sprites_environment import _resolve_labels
        assert _resolve_labels(None, False, "task1") == []

    def test_empty_labels(self):
        from sprites_environment import _resolve_labels
        assert _resolve_labels([], False, "task1") == []

    def test_dedup_preserves_order(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(["prod", "web", "prod"], False, "x")
        assert result == ["prod", "web"]

    def test_strip_whitespace(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(["  prod  ", "web"], False, "x")
        assert result == ["prod", "web"]

    def test_drop_empty_strings(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(["prod", "", "  ", "web"], False, "x")
        assert result == ["prod", "web"]

    def test_auto_tags_adds_hermes(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(None, True, "mytask")
        assert "hermes" in result

    def test_auto_tags_adds_task_slug(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(None, True, "mytask")
        assert "task-mytask" in result

    def test_auto_tags_skips_default_task(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(None, True, "default")
        assert not any(l.startswith("task-") for l in result)

    def test_auto_tags_with_static(self):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(["prod"], True, "mytask")
        assert "prod" in result
        assert "hermes" in result
        assert "task-mytask" in result

    @patch("sprites_environment._resolve_profile_identity", return_value="myprofile")
    def test_auto_tags_adds_profile(self, _mock):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(None, True, "task1")
        assert any("profile" in l for l in result)

    @patch("sprites_environment._resolve_profile_identity", return_value=None)
    def test_auto_tags_no_profile(self, _mock):
        from sprites_environment import _resolve_labels
        result = _resolve_labels(None, True, "task1")
        assert not any("profile" in l for l in result)


# ------------------------------------------------------------------ #
#  Label reconciliation                                               #
# ------------------------------------------------------------------ #

class TestReconcileLabels:
    """_reconcile_labels: merge desired into existing, additive only."""

    def _make_env(self, desired_labels):
        """Create a minimal SpritesEnvironment-like object for testing."""
        from sprites_environment import SpritesEnvironment
        env = SpritesEnvironment.__new__(SpritesEnvironment)
        env._desired_labels = desired_labels
        env._sprite = MagicMock()
        env._sprite_name = "test-sprite"
        env._sprite.labels = ["existing"]
        env._sprite.update.return_value = env._sprite
        return env

    def test_no_desired_labels_skips(self):
        env = self._make_env([])
        env._reconcile_labels()
        env._sprite.update.assert_not_called()

    def test_all_labels_present_skips(self):
        env = self._make_env(["existing"])
        env._reconcile_labels()
        env._sprite.update.assert_not_called()

    def test_adds_missing_labels(self):
        env = self._make_env(["existing", "new-tag"])
        env._reconcile_labels()
        env._sprite.update.assert_called_once()
        call_kwargs = env._sprite.update.call_args.kwargs
        assert "new-tag" in call_kwargs["labels"]
        assert "existing" in call_kwargs["labels"]

    def test_update_failure_logs_warning(self):
        env = self._make_env(["new-tag"])
        env._sprite.update.side_effect = Exception("API error")
        env._reconcile_labels()  # should not raise
        env._sprite.update.assert_called_once()

    def test_labels_attr_none_treated_as_empty(self):
        env = self._make_env(["new-tag"])
        env._sprite.labels = None
        env._reconcile_labels()
        env._sprite.update.assert_called_once()


# ------------------------------------------------------------------ #
#  Provisioning                                                       #
# ------------------------------------------------------------------ #

class TestProvisioning:
    """_provision_sprite: marker guard, fail-fast, best-effort, no-op."""

    def _make_env(self, **overrides):
        from sprites_environment import SpritesEnvironment
        env = SpritesEnvironment.__new__(SpritesEnvironment)
        env._provision_script = overrides.get("provision_script")
        env._provision_inline = overrides.get("provision_inline", "echo hello")
        env._provision_best_effort = overrides.get("provision_best_effort", False)
        env._provision_timeout = overrides.get("provision_timeout", 60)
        env._remote_home = "/root"
        env._sprite = MagicMock()
        env._sprite.name = "test-sprite"
        env._sprite_name = "test-sprite"
        env._was_created = True
        env._fs = MagicMock()
        # Make self._fs / "path" return a consistent mock file object.
        # This mock handles read_text (marker check), write_bytes (script
        # upload), and write_text (marker write).
        self._mock_file = MagicMock()
        env._fs.__truediv__.return_value = self._mock_file
        return env

    def _set_marker_absent(self, env):
        """Configure the mock so the marker file check raises (file missing)."""
        self._mock_file.read_text.side_effect = FileNotFoundError("no marker")

    def test_no_script_configured_is_noop(self):
        env = self._make_env(provision_script=None, provision_inline=None)
        env._provision_sprite()
        env._sprite.command.assert_not_called()

    def test_marker_file_skips_provisioning(self):
        env = self._make_env()
        self._mock_file.read_text.return_value = "provisioned"
        env._provision_sprite()
        env._sprite.command.assert_not_called()

    def test_marker_absent_runs_script(self):
        env = self._make_env()
        self._set_marker_absent(env)
        cmd_mock = MagicMock()
        cmd_mock.combined_output.return_value = b"hello\n"
        env._sprite.command.return_value = cmd_mock
        env._provision_sprite()
        env._sprite.command.assert_called_once()
        # marker should be written
        self._mock_file.write_text.assert_called_once_with("provisioned\n")

    def test_fail_fast_on_nonzero_exit(self):
        from sprites.exceptions import ExitError
        env = self._make_env()
        self._set_marker_absent(env)
        exit_err = ExitError("failed", 1, b"error output", b"")
        env._sprite.command.side_effect = exit_err
        with pytest.raises(RuntimeError, match="provisioning script failed"):
            env._provision_sprite()

    def test_best_effort_logs_on_failure(self):
        from sprites.exceptions import ExitError
        env = self._make_env(provision_best_effort=True)
        self._set_marker_absent(env)
        exit_err = ExitError("failed", 1, b"error output", b"")
        env._sprite.command.side_effect = exit_err
        env._provision_sprite()  # should not raise
        env._sprite.command.assert_called_once()

    def test_provision_script_path_unreadable_fail_fast(self):
        env = self._make_env(provision_script="/nonexistent/script.sh", provision_inline=None)
        self._set_marker_absent(env)
        with pytest.raises(RuntimeError, match="provision script not readable"):
            env._provision_sprite()

    def test_provision_script_path_unreadable_best_effort(self):
        env = self._make_env(
            provision_script="/nonexistent/script.sh",
            provision_inline=None,
            provision_best_effort=True,
        )
        self._set_marker_absent(env)
        env._provision_sprite()  # should not raise
        env._sprite.command.assert_not_called()

    def test_inline_script_uploaded_and_executed(self):
        env = self._make_env(provision_inline="echo 'provisioning'")
        self._set_marker_absent(env)
        cmd_mock = MagicMock()
        cmd_mock.combined_output.return_value = b"provisioning\n"
        env._sprite.command.return_value = cmd_mock
        env._provision_sprite()
        # script should be written to the filesystem
        self._mock_file.write_bytes.assert_called_once()
        written_bytes = self._mock_file.write_bytes.call_args[0][0]
        assert b"provisioning" in written_bytes


# ------------------------------------------------------------------ #
#  Provider config threading                                         #
# ------------------------------------------------------------------ #

class TestProviderConfig:
    """SpritesProvider.create_environment threads sprites config correctly."""

    def _make_provider(self, plugin_settings=None):
        """Build a SpritesProvider with _plugin_* attrs set as if register() ran."""
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()
        if plugin_settings:
            normalise = provider_mod._normalise_tags
            raw = plugin_settings.get("tags")
            provider._plugin_labels = normalise(raw) if raw is not None else None
            val = plugin_settings.get("auto_tags")
            provider._plugin_auto_tags = bool(val) if val is not None else None
            val = plugin_settings.get("provision_script")
            provider._plugin_provision_script = str(val) if val else None
            val = plugin_settings.get("provision_inline")
            provider._plugin_provision_inline = str(val) if val else None
            val = plugin_settings.get("provision_best_effort")
            provider._plugin_provision_best_effort = bool(val) if val is not None else None
            val = plugin_settings.get("provision_timeout")
            provider._plugin_provision_timeout = int(val) if val is not None else None
        return provider

    def test_plugin_settings_win_over_terminal_sprites(self):
        """Settings from plugins.entries.sprites.settings.* take priority."""
        provider = self._make_provider({"tags": ["plugin-tag"], "auto_tags": True})
        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root", timeout=60, task_id="test",
                container_config={"sprites": {"tags": ["ignored"], "auto_tags": False}},
            )
        kw = mock_env.call_args.kwargs
        assert kw["labels"] == ["plugin-tag"]
        assert kw["auto_tags"] is True

    def test_plugin_provision_settings_win_over_terminal_sprites(self):
        provider = self._make_provider({
            "provision_script": "/plugin/script.sh",
            "provision_best_effort": True,
            "provision_timeout": 120,
        })
        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root", timeout=60, task_id="test",
                container_config={"sprites": {
                    "provision_script": "/ignored.sh",
                    "provision_best_effort": False,
                    "provision_timeout": 999,
                }},
            )
        kw = mock_env.call_args.kwargs
        assert kw["provision_script"] == "/plugin/script.sh"
        assert kw["provision_best_effort"] is True
        assert kw["provision_timeout"] == 120

    def test_plugin_false_auto_tags_not_overridden_by_terminal(self):
        """Explicitly configured False in plugin settings must not fall through."""
        provider = self._make_provider({"auto_tags": False})
        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root", timeout=60, task_id="test",
                container_config={"sprites": {"auto_tags": True}},
            )
        assert mock_env.call_args.kwargs["auto_tags"] is False

    def test_threads_labels_from_sprites_subsection(self):
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()

        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root",
                timeout=60,
                task_id="test",
                container_config={
                    "container_persistent": True,
                    "sprites": {
                        "tags": ["prod", "web"],
                        "auto_tags": True,
                        "provision_script": "/path/to/provision.sh",
                        "provision_best_effort": True,
                        "provision_timeout": 300,
                    },
                },
            )
        call_kwargs = mock_env.call_args.kwargs
        assert call_kwargs["labels"] == ["prod", "web"]
        assert call_kwargs["auto_tags"] is True
        assert call_kwargs["provision_script"] == "/path/to/provision.sh"
        assert call_kwargs["provision_best_effort"] is True
        assert call_kwargs["provision_timeout"] == 300
        assert call_kwargs["persistent_filesystem"] is True

    def test_threads_flat_keys_as_fallback(self):
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()

        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root",
                timeout=60,
                task_id="test",
                container_config={
                    "container_persistent": False,
                    "sprites_tags": ["dev"],
                },
            )
        call_kwargs = mock_env.call_args.kwargs
        assert call_kwargs["labels"] == ["dev"]
        assert call_kwargs["persistent_filesystem"] is False

    def test_empty_tags_list_is_preserved_not_fallen_through(self):
        """An explicit tags: [] must not fall through to the flat-key fallback."""
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()

        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root",
                timeout=60,
                task_id="test",
                container_config={
                    "sprites": {"tags": []},
                    "sprites_tags": ["should-not-appear"],
                },
            )
        call_kwargs = mock_env.call_args.kwargs
        assert call_kwargs["labels"] == []

    def test_string_tags_json_parsed(self):
        """tags passed as a JSON string (e.g. from hermes config set) are parsed."""
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()

        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root",
                timeout=60,
                task_id="test",
                container_config={"sprites": {"tags": '["prod", "web"]'}},
            )
        call_kwargs = mock_env.call_args.kwargs
        assert call_kwargs["labels"] == ["prod", "web"]

    def test_string_tags_csv_parsed(self):
        """tags passed as a comma-separated string are split into a list."""
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()

        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root",
                timeout=60,
                task_id="test",
                container_config={"sprites": {"tags": "prod, web"}},
            )
        call_kwargs = mock_env.call_args.kwargs
        assert call_kwargs["labels"] == ["prod", "web"]

    def test_defaults_when_no_sprites_config(self):
        import importlib
        provider_mod = importlib.import_module("__init__")
        provider = provider_mod.SpritesProvider()

        with patch("sprites_environment.SpritesEnvironment") as mock_env:
            provider.create_environment(
                cwd="/root",
                timeout=60,
                task_id="test",
                container_config={},
            )
        call_kwargs = mock_env.call_args.kwargs
        assert call_kwargs["labels"] is None
        assert call_kwargs["auto_tags"] is False
        assert call_kwargs["provision_script"] is None
        assert call_kwargs["provision_inline"] is None
        assert call_kwargs["provision_best_effort"] is False
        assert call_kwargs["provision_timeout"] == 600