"""Registry override, its fallbacks, and the version gate.

These drive the real ``PlatformRegistry`` and the real
``PluginContext.register_platform``, not stand-ins — the question they answer
is "can an external platform plugin safely replace the built-in Discord
adapter, on this Hermes build, without ever leaving the gateway without a
Discord platform?"
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import replace
from unittest.mock import patch

import pytest
from conftest import HERMES_ROOT, PLUGIN_DIR, load_plugin_package

plugin = load_plugin_package()
compat = plugin.compat


def _register_now(ctx):
    """Register and look up the adapter through the real registry."""
    from gateway.platform_registry import platform_registry

    plugin.register(ctx)
    return platform_registry.get(plugin.PLATFORM_ID)


def _builtin_ctx(plugin_context):
    return plugin_context(name="discord-platform", source="bundled")


def _unsupported() -> compat.CoreCompat:
    return compat.CoreCompat(
        module_name="plugins.platforms.discord.adapter",
        missing_required=("DiscordAdapter._discord_message_admission",),
        missing_optional=(),
        has_gate_raw=False,
        has_allow_bots_hook=False,
        has_gate_env_snapshot=False,
        has_channel_gate_helpers=False,
    )


# ── the override itself ─────────────────────────────────────────────────────


class TestRegistryOverride:
    def test_registers_under_the_discord_platform_id(self, registry, plugin_context):
        assert _register_now(plugin_context()).adapter_factory is not None
        assert plugin.PLATFORM_ID == "discord"

    def test_registration_uses_the_active_profile_scope(self, registry, plugin_context):
        plugin.register(plugin_context())
        entry, loader = registry.snapshot_registration(
            "discord", scope=registry.current_scope_key()
        )
        assert loader is None
        assert "hermes_discord_relay" in entry.adapter_factory.__module__
        assert registry.snapshot_registration("discord") == (None, None)

    def test_factory_builds_the_custom_adapter(self, registry, plugin_context, tmp_path):
        from gateway.config import PlatformConfig

        entry = _register_now(plugin_context())
        cfg = PlatformConfig(enabled=True, token="t")
        cfg.extra = {}
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter = entry.adapter_factory(cfg)
        assert type(adapter).__name__ == "DiscordCustomAdapter"

    def test_the_custom_adapter_is_a_builtin_discord_adapter(
        self, registry, plugin_context, tmp_path
    ):
        """Inheritance, not reimplementation."""
        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        entry = _register_now(plugin_context())
        cfg = PlatformConfig(enabled=True, token="t")
        cfg.extra = {}
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter = entry.adapter_factory(cfg)
        assert isinstance(adapter, DiscordAdapter)

    def test_a_pending_deferred_builtin_loader_is_dropped(self, registry, plugin_context):
        """The bundled Discord plugin registers lazily; we must win cleanly."""
        calls = []
        registry.register_deferred(
            "discord", lambda: calls.append(1), scope=registry.current_scope_key()
        )
        plugin.register(plugin_context())
        assert registry.get("discord") is not None
        assert calls == []          # the bundled loader never ran

    def test_override_wins_when_the_builtin_registered_first(self, registry, plugin_context):
        """Discovery order must not decide the winner."""
        from plugins.platforms.discord.adapter import register as builtin_register

        builtin_register(_builtin_ctx(plugin_context))
        before = registry.get("discord").adapter_factory
        after = _register_now(plugin_context()).adapter_factory
        assert before is not after
        assert "hermes_discord_relay" in after.__module__

    def test_entry_is_marked_as_a_plugin_source(self, registry, plugin_context):
        entry = _register_now(plugin_context())
        assert entry.source == "plugin"
        assert entry.plugin_name == "hermes-discord-relay"


# ── inheritance of the built-in entry ───────────────────────────────────────


class TestInheritedEntryFields:
    def _pair(self, registry, plugin_context):
        from plugins.platforms.discord.adapter import register as builtin_register

        builtin_register(_builtin_ctx(plugin_context))
        builtin = registry.get("discord")
        return builtin, _register_now(plugin_context())

    @pytest.mark.parametrize(
        "field",
        [
            "label",
            "check_fn",
            "is_connected",
            "required_env",
            "setup_fn",
            "allowed_users_env",
            "allow_all_env",
            "cron_deliver_env_var",
            "standalone_sender_fn",
            "max_message_length",
            "emoji",
            "allow_update_command",
            "install_hint",
        ],
    )
    def test_field_is_inherited_verbatim(self, registry, plugin_context, field):
        builtin, custom = self._pair(registry, plugin_context)
        assert getattr(custom, field) == getattr(builtin, field)

    def test_only_the_two_intended_callables_differ(self, registry, plugin_context):
        """Guards against the override quietly dropping a future entry field."""
        import dataclasses

        builtin, custom = self._pair(registry, plugin_context)
        differing = {
            f.name
            for f in dataclasses.fields(builtin)
            if getattr(builtin, f.name) != getattr(custom, f.name)
        }
        # plugin_name legitimately differs: the bundled entry is registered by
        # the "discord-platform" manifest, ours by this plugin's manifest.
        assert differing == {
            "adapter_factory",
            "apply_yaml_config_fn",
            "plugin_name",
        }

    def test_credentials_are_reused_unchanged(self, registry, plugin_context):
        _, custom = self._pair(registry, plugin_context)
        assert custom.required_env == ["DISCORD_BOT_TOKEN"]
        assert custom.allowed_users_env == "DISCORD_ALLOWED_USERS"


class TestCapturingContext:
    """The shim must survive a built-in ``register()`` that grows a new call."""

    def test_it_captures_the_platform_kwargs(self):
        captured = plugin._CapturingContext()
        captured.register_platform(name="discord", label="Discord")
        assert captured.kwargs["label"] == "Discord"

    def test_an_unexpected_context_call_does_not_raise(self, caplog):
        captured = plugin._CapturingContext()
        with caplog.at_level(logging.WARNING):
            captured.register_tool("something_new", lambda: None)
        assert captured.kwargs is None
        assert "register_tool" in caplog.text

    def test_an_unexpected_attribute_access_does_not_raise(self):
        captured = plugin._CapturingContext()
        assert captured.some_future_hook is not None


# ── the version gate ────────────────────────────────────────────────────────


class TestVersionCompatibilityGate:
    def test_an_unsupported_core_falls_back_to_the_builtin_adapter(
        self, registry, plugin_context
    ):
        with patch.object(plugin.compat, "probe", return_value=_unsupported()):
            plugin.register(plugin_context())
        entry = registry.get("discord")
        assert entry is not None
        assert "hermes_discord_relay" not in entry.adapter_factory.__module__
        assert entry.required_env == ["DISCORD_BOT_TOKEN"]

    def test_an_unsupported_core_is_reported_at_error_level(
        self, registry, plugin_context, caplog
    ):
        with caplog.at_level(logging.ERROR), patch.object(
            plugin.compat, "probe", return_value=_unsupported()
        ):
            plugin.register(plugin_context())
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors
        assert "_discord_message_admission" in caplog.text

    def test_a_missing_optional_seam_is_only_a_warning(
        self, registry, plugin_context, caplog
    ):
        support = replace(
            compat.probe(), missing_optional=("DiscordAdapter._get_allow_bots",),
            has_allow_bots_hook=False,
        )
        with caplog.at_level(logging.WARNING), patch.object(
            plugin.compat, "probe", return_value=support
        ):
            plugin.register(plugin_context())
        entry = registry.get("discord")
        assert "hermes_discord_relay" in entry.adapter_factory.__module__
        assert "_get_allow_bots" in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_a_missing_builtin_yaml_bridge_declines_the_override(
        self, registry, plugin_context
    ):
        import plugins.platforms.discord.adapter as builtin

        with patch.object(builtin, "_apply_yaml_config", None):
            plugin.register(plugin_context())
        entry = registry.get("discord")
        assert "hermes_discord_relay" not in entry.adapter_factory.__module__


# ── failure isolation ───────────────────────────────────────────────────────


class TestFallback:
    def test_a_broken_override_falls_back_to_the_builtin_adapter(
        self, registry, plugin_context
    ):
        with patch.object(plugin, "_CapturingContext", side_effect=RuntimeError("boom")):
            plugin.register(plugin_context())
        entry = registry.get("discord")
        assert entry is not None
        assert "hermes_discord_relay" not in entry.adapter_factory.__module__
        assert entry.required_env == ["DISCORD_BOT_TOKEN"]

    def test_register_never_raises(self, registry, plugin_context, caplog):
        with patch.object(plugin, "_install_override", side_effect=RuntimeError("boom")):
            plugin.register(plugin_context())
        assert registry.get("discord").adapter_factory.__module__ == "plugins.platforms.discord.adapter"
        assert "failed to install its override" in caplog.text

    def test_a_broken_builtin_fallback_is_reported(self, registry, plugin_context, caplog):
        with patch.object(plugin, "_CapturingContext", side_effect=RuntimeError("boom")), patch(
            "plugins.platforms.discord.adapter.register", side_effect=RuntimeError("builtin broken")
        ):
            plugin.register(plugin_context())
        assert "could not be registered either" in caplog.text
        assert not registry.is_registered("discord")


# ── the lazy discord.py dependency ──────────────────────────────────────────


class TestLazyDiscordDependency:
    """discord.py is a lazily-installed extra, not a core dependency."""

    def test_the_plugin_registers_with_discord_uninstalled(self):
        """A bare ``import discord`` here would silently disable the feature.

        Run in a subprocess with ``discord`` blocked at import time — the shape
        of a host that has not lazily installed the extra yet.
        """
        script = f"""
import importlib.abc, importlib.util, json, sys, types

class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "discord" or name.startswith("discord."):
            raise ImportError("blocked for test")
        return None

sys.meta_path.insert(0, Blocker())
sys.path.insert(0, {str(HERMES_ROOT)!r})

ns = types.ModuleType("hp"); ns.__path__ = []; sys.modules["hp"] = ns
spec = importlib.util.spec_from_file_location(
    "hp.plugin", {str(PLUGIN_DIR / "__init__.py")!r},
    submodule_search_locations=[{str(PLUGIN_DIR)!r}])
m = importlib.util.module_from_spec(spec)
m.__package__ = "hp.plugin"; m.__path__ = [{str(PLUGIN_DIR)!r}]
sys.modules["hp.plugin"] = m
spec.loader.exec_module(m)

from gateway.platform_registry import platform_registry

class Ctx:
    def __init__(self): self.kwargs = None
    def register_platform(self, **kw):
        self.kwargs = kw
        from gateway.platform_registry import PlatformEntry
        platform_registry.register(PlatformEntry(**{{
            k: v for k, v in kw.items()
            if k in {{f.name for f in __import__("dataclasses").fields(PlatformEntry)}}
        }}))

ctx = Ctx()
m.register(ctx)
entry = platform_registry.get("discord")
adapter_mod = importlib.import_module("hp.plugin.adapter")
print(json.dumps({{
    "discord_importable": adapter_mod.compat.discord_module() is None,
    "adapter_module_loaded": True,
    "factory_module": entry.adapter_factory.__module__,
}}))
"""
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(HERMES_ROOT),
        )
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["discord_importable"] is True   # discord really was blocked
        assert result["adapter_module_loaded"] is True
        assert "hp.plugin" in result["factory_module"]
