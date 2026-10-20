"""Cold native loader, actual selected factory and durable receipts over unload/reload."""

import os
import subprocess
import sys
import textwrap

from conftest import HERMES_ROOT, PLUGIN_DIR


def test_native_lifecycle_dispatch_and_compatible_old_config(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:\n  load_timeout_seconds: 60\n")
    script = textwrap.dedent(f"""
        import asyncio
        import os
        import sys
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock
        sys.path.insert(0, {str(HERMES_ROOT)!r})
        sys.path.insert(0, {str(PLUGIN_DIR / "tests")!r})
        from gateway.config import PlatformConfig
        from gateway.platform_registry import platform_registry as registry
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_manifest import parse_manifest_file
        from hermes_cli.plugins_loader import _plugin_home_scope

        root = Path({str(PLUGIN_DIR)!r})
        core = Path({str(HERMES_ROOT)!r})
        manager = PluginManager()
        builtin_dir = core / 'plugins/platforms/discord'
        builtin = parse_manifest_file(builtin_dir / 'plugin.yaml', builtin_dir, 'bundled', '')
        custom = parse_manifest_file(root / 'plugin.yaml', root, 'user', '')
        manager._register_deferred_platform(builtin)
        before = registry.snapshot_registration('discord', scope=manager.scope_key)

        async def dispatch(entry, *, repeated):
            from test_adapter import _user
            from test_relay_adapter import relay_config, relay_message
            for old in (True, False, {{'enabled': True, 'bot_handoff': 'off'}}):
                block = {{'thread_ownership_transfer': old}}
                cfg = PlatformConfig(enabled=True, token='offline-test')
                cfg.extra = entry.apply_yaml_config_fn({{'discord': block}}, block)
                legacy = entry.adapter_factory(cfg)
                assert legacy._ownership_config().enabled == bool(old)
                assert 'relay' not in legacy.config.extra
                assert not legacy._relay_receipts.path.exists() or repeated
                await legacy.disconnect()
            block = {{'relay': relay_config(), 'thread_ownership_transfer': False}}
            cfg = PlatformConfig(enabled=True, token='offline-test')
            cfg.extra = entry.apply_yaml_config_fn({{'discord': block}}, block)
            adapter = entry.adapter_factory(cfg)
            assert adapter.__class__.__name__ == 'DiscordCustomAdapter'
            adapter._client = MagicMock()
            adapter._client.user = _user(1000, bot=True)
            adapter._client.get_user = lambda uid: None
            adapter._allowed_user_ids = {{'9001', '2000'}}
            adapter._allowed_role_ids = set()
            adapter.set_authorization_check(lambda *args, **kwargs: True)
            adapter._ready_event.set()
            adapter._text_batch_delay_seconds = 10
            adapter._collect_attachment_media = AsyncMock(return_value=([], [], False, ''))
            adapter._discord_history_backfill = lambda: False
            adapter._threads.mark_async = AsyncMock()
            adapter.handle_message = AsyncMock()
            assert await adapter._dispatch_discord_message(relay_message(message_id=101 if repeated else 100)) == (not repeated)
            if not repeated:
                event = adapter.handle_message.call_args.args[0]
                assert event.source.user_id == '2000' and event.source.is_bot is True
                assert event.source.role_authorized is False
                assert event.source.thread_id == '77'
                assert 'report the result or blocker to the human here' in event.text
                assert 'not an authenticated human identity or approval' in event.text
            else:
                adapter.handle_message.assert_not_awaited()
            adapter._client = None
            await adapter.disconnect()

        for repeated in (False, True):
            manager._load_plugin(custom)
            loaded = manager._plugins[custom.name]
            assert loaded.enabled and loaded.module is not None and not loaded.error, loaded.error
            with _plugin_home_scope(manager.home_path):
                entry = registry.get('discord')
                assert entry.plugin_name == custom.name
                assert 'hermes_discord_thread_ownership' in entry.adapter_factory.__module__
                asyncio.run(dispatch(entry, repeated=repeated))
            assert manager.unload(custom.name)
            assert registry.snapshot_registration('discord', scope=manager.scope_key) == before
        manager.unload()
        print('PASS: native factory, legacy config, relay dispatch, unload/reload, durable replay denial')
    """)
    env = dict(
        os.environ,
        HERMES_HOME=str(home),
        DISCORD_ALLOW_BOTS="none",
        DISCORD_THREAD_OWNERSHIP_TRANSFER="",
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    print(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
