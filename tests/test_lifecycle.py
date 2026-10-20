"""Cold-process checks through the stock Hermes loader and ownership ledger."""

import os
import subprocess
import sys
import textwrap

import pytest
from conftest import HERMES_ROOT, PLUGIN_DIR


@pytest.mark.parametrize("materialized", [False, True])
def test_override_is_owned_by_the_profile_lifecycle(tmp_path, materialized):
    # Acceptance runs with 25% CPU, so cold imports need a larger wall deadline.
    for home in (tmp_path / "home", tmp_path / "other"):
        home.mkdir()
        (home / "config.yaml").write_text("plugins:\n  load_timeout_seconds: 60\n")
    script = textwrap.dedent(f"""
        import inspect
        import logging
        import sys
        from pathlib import Path

        sys.path.insert(0, {str(HERMES_ROOT)!r})
        from gateway.platform_registry import platform_registry as registry
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        from hermes_cli.plugins_loader import _plugin_home_scope, _resolve_plugin_load_timeout

        def lookup(owner):
            with _plugin_home_scope(owner.home_path):
                assert _resolve_plugin_load_timeout() == 60
                return registry.get('discord')

        print('runtime:', sys.executable, sys.version, flush=True)
        logging.basicConfig(level=logging.INFO)
        root = Path({str(PLUGIN_DIR)!r})
        core = Path({str(HERMES_ROOT)!r})
        manager = PluginManager()
        other = PluginManager(scope_key=str(Path({str(tmp_path)!r}) / 'other'))
        builtin_dir = core / 'plugins/platforms/discord'
        builtin = parse_manifest_file(
            builtin_dir / 'plugin.yaml', builtin_dir, 'bundled', '')
        custom = parse_manifest_file(root / 'plugin.yaml', root, 'user', '')
        assert builtin is not None and custom is not None
        manager._register_deferred_platform(builtin)
        other._register_deferred_platform(builtin)
        if {materialized!r}:
            lookup(manager)
        before = registry.snapshot_registration('discord', scope=manager.scope_key)
        manager._load_plugin(custom)
        loaded = manager._plugins[custom.name]
        assert loaded.enabled and not loaded.error, loaded.error
        entry = lookup(manager)
        print('selected factory:', entry.adapter_factory.__module__, flush=True)
        assert 'hermes_discord_relay' in entry.adapter_factory.__module__, (
            'enabled ownership plugin did not replace the built-in adapter')
        assert entry.plugin_name == 'hermes-discord-relay'
        assert registry.snapshot_registration('discord') == (None, None), (
            'plugin leaked a process-global registration')
        other_entry = lookup(other)
        assert other_entry.adapter_factory.__name__ == 'DiscordAdapter'
        assert Path(inspect.getfile(other_entry.adapter_factory)).resolve() == builtin_dir / 'adapter.py'
        assert manager.unload(custom.name)
        assert registry.snapshot_registration('discord', scope=manager.scope_key) == before
        restored = lookup(manager)
        assert restored.adapter_factory.__name__ == 'DiscordAdapter'
        assert Path(inspect.getfile(restored.adapter_factory)).resolve() == builtin_dir / 'adapter.py'
        assert lookup(other) is other_entry
        manager._load_plugin(custom)
        assert 'hermes_discord_relay' in lookup(manager).adapter_factory.__module__
        manager.unload()
        other.unload()
        assert registry.snapshot_registration('discord', scope=manager.scope_key) == (None, None)
        assert registry.snapshot_registration('discord', scope=other.scope_key) == (None, None)
        print('PASS: override, profile isolation, unload, reload, and builtin restoration')
    """)
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=180,
    )
    print(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
