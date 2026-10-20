"""YAML → ``PlatformConfig.extra`` bridge.

The bridge's job is to run the built-in Discord bridge first — so all ~23
existing ``discord:`` keys keep working — and then add this plugin's one key.
The dangerous failure mode is the quiet one: losing the built-in half and
applying only ours, which would silently drop ``require_mention``,
``auto_thread``, the channel allow/ignore lists, reactions and backfill with
nothing but a warning. These tests pin the loud behaviour instead.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest
from conftest import load_plugin_module

bridge = load_plugin_module("config_bridge")
compat = load_plugin_module("compat")


def _apply(discord_block, yaml_cfg=None):
    if yaml_cfg is None:
        yaml_cfg = {"discord": discord_block}
    return bridge.apply_yaml_config(yaml_cfg, discord_block)


class TestOwnershipKey:
    def test_absent_key_yields_no_ownership_extra(self):
        assert "thread_ownership_transfer" not in _apply({})

    def test_scalar_true_is_passed_through(self):
        extra = _apply({"thread_ownership_transfer": True})
        assert extra["thread_ownership_transfer"] is True

    def test_mapping_is_passed_through(self):
        raw = {"enabled": True, "bot_handoff": "mention"}
        assert _apply({"thread_ownership_transfer": raw})[
            "thread_ownership_transfer"
        ] == raw

    def test_nested_only_config_is_picked_up(self):
        """``platforms.discord:`` with no top-level ``discord:`` block."""
        block = {"thread_ownership_transfer": True}
        extra = _apply(block, yaml_cfg={"platforms": {"discord": block}})
        assert extra["thread_ownership_transfer"] is True

    def test_top_level_block_is_picked_up_when_the_hook_arg_is_empty(self):
        extra = _apply({}, yaml_cfg={"discord": {"thread_ownership_transfer": True}})
        assert extra["thread_ownership_transfer"] is True


class TestProfileSafety:
    def test_never_writes_process_global_env(self):
        """``extra`` is per-adapter; ``os.environ`` is shared by every profile."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_THREAD_OWNERSHIP_TRANSFER", None)
            _apply({"thread_ownership_transfer": True})
            assert "DISCORD_THREAD_OWNERSHIP_TRANSFER" not in os.environ

    def test_existing_env_var_is_left_alone(self):
        with patch.dict(os.environ, {"DISCORD_THREAD_OWNERSHIP_TRANSFER": "false"}):
            _apply({"thread_ownership_transfer": True})
            assert os.environ["DISCORD_THREAD_OWNERSHIP_TRANSFER"] == "false"


class TestBuiltinInheritance:
    def test_builtin_discord_keys_still_bridge(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISCORD_REQUIRE_MENTION", None)
            _apply({"require_mention": False})
            assert os.environ["DISCORD_REQUIRE_MENTION"] == "false"

    def test_builtin_extras_are_preserved(self):
        from plugins.platforms.discord.adapter import _apply_yaml_config as builtin

        expected = builtin({"discord": {}}, {}) or {}
        got = _apply({})
        for key, value in expected.items():
            assert got[key] == value


class TestLoudFailure:
    """A broken built-in bridge must never degrade to a partial config."""

    def test_a_raising_builtin_bridge_propagates(self):
        import plugins.platforms.discord.adapter as builtin

        with patch.object(builtin, "_apply_yaml_config", side_effect=RuntimeError):
            with pytest.raises(RuntimeError):
                bridge.apply_yaml_config({}, {"thread_ownership_transfer": True})

    def test_a_raising_builtin_bridge_is_logged_at_error(self, caplog):
        import plugins.platforms.discord.adapter as builtin

        with caplog.at_level(logging.ERROR):
            with patch.object(builtin, "_apply_yaml_config", side_effect=RuntimeError):
                with pytest.raises(RuntimeError):
                    bridge.apply_yaml_config({}, {})
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_our_key_is_not_applied_when_the_builtin_half_failed(self):
        """Partial config is the failure the audit called out; refuse it."""
        import plugins.platforms.discord.adapter as builtin

        with patch.object(builtin, "_apply_yaml_config", side_effect=RuntimeError):
            try:
                bridge.apply_yaml_config({}, {"thread_ownership_transfer": True})
            except RuntimeError:
                pass
            else:  # pragma: no cover - guarded by the test above
                pytest.fail("expected the bridge to propagate")

    def test_a_missing_builtin_bridge_is_an_incompatibility(self):
        import plugins.platforms.discord.adapter as builtin

        with patch.object(builtin, "_apply_yaml_config", None):
            with pytest.raises(compat.IncompatibleHermesError):
                bridge.resolve_builtin_bridge()

    def test_resolve_returns_the_real_builtin(self):
        from plugins.platforms.discord.adapter import _apply_yaml_config as builtin

        assert bridge.resolve_builtin_bridge() is builtin


class TestRelayBridge:
    def test_relay_and_legacy_ownership_bridge_together(self):
        raw = {"enabled": True, "allow": [
            {"sender_id": "2000", "guild_id": "4242", "thread_id": "77"}]}
        block = {"relay": raw, "thread_ownership_transfer": True}
        extra = _apply(block)
        assert extra["relay"] == raw
        assert extra["thread_ownership_transfer"] is True
        nested = bridge.apply_yaml_config({"platforms": {"discord": block}}, block)
        assert nested["relay"] == raw
        assert bridge.apply_yaml_config({"discord": block}, {})["relay"] == raw

    def test_invalid_relay_is_rejected_before_any_native_config_is_applied(self):
        import plugins.platforms.discord.adapter as builtin
        with patch.object(builtin, "_apply_yaml_config") as apply:
            with pytest.raises(ValueError):
                _apply({"relay": {"enabled": True, "allow": "*"}})
            apply.assert_not_called()

    def test_raising_native_bridge_does_not_apply_relay(self):
        import plugins.platforms.discord.adapter as builtin
        with patch.object(builtin, "_apply_yaml_config", side_effect=RuntimeError):
            with pytest.raises(RuntimeError):
                _apply({"relay": {"enabled": True}})

    def test_relay_bridge_never_sets_global_bot_policy(self, monkeypatch):
        monkeypatch.setenv("DISCORD_ALLOW_BOTS", "none")
        _apply({"relay": {"enabled": True}})
        assert os.environ["DISCORD_ALLOW_BOTS"] == "none"
