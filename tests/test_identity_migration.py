"""Execute the documented source migration and rollback through native discovery."""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap

import pytest
import yaml
from conftest import HERMES_ROOT, PLUGIN_DIR

BASELINE = "f59438b6c3c9f7ad58bf4e1cd211ac7cf574445b"
OLD = "hermes-discord-thread-ownership"
NEW = "hermes-discord-relay"


def run_documented_step(name, env):
    guide = (PLUGIN_DIR / "docs/migrate-to-discord-relay.md").read_text()
    block = re.search(rf"<!-- migration-{name} -->\s*```bash\n(.*?)\n```", guide, re.S)
    assert block is not None
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", block[1]], env=env,
        capture_output=True, text=True, timeout=60,
    )
    print(result.stdout + result.stderr)
    return result


def native_probe(home, other, identity, *, mode="verify", enabled=True):
    script = textwrap.dedent(f"""
        import asyncio
        import importlib
        import sys
        from pathlib import Path
        import yaml
        sys.path.insert(0, {str(HERMES_ROOT)!r})
        from gateway.config import PlatformConfig
        from gateway.platform_registry import platform_registry as registry
        from hermes_cli.plugins import PluginManager
        from hermes_cli.plugins_loader import _plugin_home_scope

        home = Path({str(home)!r})
        manager = PluginManager(scope_key=str(home))
        other = PluginManager(scope_key={str(other)!r})
        other.discover_and_load()
        with _plugin_home_scope(other.home_path):
            other_entry = registry.get('discord')
            assert other_entry.adapter_factory.__name__ == 'DiscordAdapter'
        manager.discover_and_load()
        with _plugin_home_scope(home):
            entry = registry.get('discord')
            if not {enabled!r}:
                assert entry.adapter_factory.__name__ == 'DiscordAdapter'
                assert not manager._plugins[{identity!r}].enabled
            else:
                assert entry.plugin_name == {identity!r}
                assert entry.adapter_factory.__module__ == 'hermes_plugins.' + {identity!r}.replace('-', '_') + '.adapter'
                assert sum(p.enabled and p.manifest.name in ({OLD!r}, {NEW!r}) for p in manager._plugins.values()) == 1
                cfg = PlatformConfig(enabled=True, token='offline')
                block = yaml.safe_load((home / 'config.yaml').read_text())['discord']
                cfg.extra = entry.apply_yaml_config_fn({{'discord': block}}, block)
                adapter = entry.adapter_factory(cfg)
                raw = block['thread_ownership_transfer']
                expected = raw.get('enabled', True) if isinstance(raw, dict) else raw
                assert adapter._ownership_config().enabled == expected
                package = entry.adapter_factory.__module__.rsplit('.', 1)[0]
                relay = importlib.import_module(package + '.relay')
                assert relay.RelayConfig.parse(cfg.extra['relay']).enabled is True
                ownership = importlib.import_module(package + '.ownership')
                if {mode!r} == 'seed':
                    facts = ownership.MessageFacts(thread_id='77', message_id='100', author_id='9001', author_is_bot=False,
                                                   self_id='1000', mentioned_bot_ids=('1000',), content='<@1000>')
                    assert adapter._ownership_store.apply(facts, ownership.OwnershipConfig(enabled=True), now=1800000000).owner_id == '1000'
                assert adapter._ownership_store.owner_of('77') == '1000'
                receipt = dict(sender_id='2000', recipient_id='1000', request_id='a' * 32, message_id='101', now=1800000001)
                assert adapter._relay_receipts.path == home / 'discord_relay_receipts.sqlite3'
                assert adapter._relay_receipts.reserve(**receipt) == ({mode!r} == 'seed')
                if {identity!r} == {NEW!r}:
                    receipt.update(request_id='b' * 32, message_id='102')
                    assert adapter._relay_receipts.reserve(**receipt)
                elif {mode!r} == 'rollback':
                    receipt.update(request_id='b' * 32, message_id='102')
                    assert not adapter._relay_receipts.reserve(**receipt)
                asyncio.run(adapter.disconnect())
                assert manager.unload({identity!r})
                assert registry.get('discord').adapter_factory.__name__ == 'DiscordAdapter'
                manager.discover_and_load(force=True)
                assert registry.get('discord').plugin_name == {identity!r}
        with _plugin_home_scope(other.home_path):
            assert registry.get('discord') is other_entry
        assert not (other.home_path / 'discord_thread_ownership.json').exists()
        assert not (other.home_path / 'discord_relay_receipts.sqlite3').exists()
        manager.unload()
        other.unload()
        assert registry.snapshot_registration('discord') == (None, None)
        print('PASS native discovery, selected factory, profile isolation, stores, unload/reload:', {identity!r}, {mode!r}, {enabled!r})
    """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, HERMES_HOME=str(home), DISCORD_THREAD_OWNERSHIP_TRANSFER="",
                 DISCORD_ALLOW_BOTS="none"),
        cwd=home.parent, capture_output=True, text=True, timeout=180,
    )
    print(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("ownership,disabled", [(True, False), (False, False), ({"enabled": True, "bot_handoff": "off"}, False), (True, True)])
def test_documented_identity_migration_preserves_profiles_and_rollback(tmp_path, ownership, disabled):
    home, other = tmp_path / "profile", tmp_path / "other"
    old = home / "plugins" / OLD
    old.mkdir(parents=True)
    other.mkdir()
    (other / "config.yaml").write_text("plugins:\n  load_timeout_seconds: 60\n")
    archive = subprocess.check_output(["git", "archive", BASELINE], cwd=PLUGIN_DIR)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(old, filter="data")
    cfg = {
        "plugins": {"enabled": [OLD], "disabled": [], "load_timeout_seconds": 60},
        "discord": {"thread_ownership_transfer": ownership, "allow_bots": "none",
                    "relay": {"enabled": True, "allow": [{"sender_id": "2000", "guild_id": "4242", "thread_id": "77"}]}},
        "unrelated": {"literal": OLD},
    }
    config = home / "config.yaml"
    config.write_text(yaml.safe_dump(cfg))
    metadata = home / "plugins" / ".install-metadata.json"
    original_records = {OLD: {"source": "https://github.com/dmmeteo/hermes-discord-thread-ownership.git", "revision": BASELINE, "pinned": True}, "another-plugin": {"pinned": False}}
    metadata.write_text(json.dumps(original_records))
    native_probe(home, other, OLD, mode="seed")
    if disabled:
        cfg["plugins"]["disabled"] = [OLD]
        config.write_text(yaml.safe_dump(cfg))
    original_config = config.read_bytes()
    original_metadata = metadata.read_bytes()
    state = home / "discord_thread_ownership.json"
    state_bytes = state.read_bytes()
    receipts = home / "discord_relay_receipts.sqlite3"
    receipt_bytes = receipts.read_bytes()
    source = tmp_path / "source"
    shutil.copytree(PLUGIN_DIR, source, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache"))
    for command in (["git", "init", "-q", str(source)], ["git", "-C", str(source), "add", "."],
                    ["git", "-C", str(source), "-c", "user.name=Scratch", "-c", "user.email=scratch@example.invalid", "commit", "-qm", "Fixture of current source"]):
        subprocess.run(command, check=True, capture_output=True)
    env = dict(os.environ, PROFILE_HOME=str(home), CANDIDATE_DIR=str(source),
               MIGRATION_BACKUP=str(tmp_path / "backup"), MIGRATION_PYTHON=sys.executable)
    assert run_documented_step("apply", env).returncode == 0
    migrated = yaml.safe_load(config.read_text())
    assert migrated["discord"] == cfg["discord"]
    assert migrated["unrelated"] == cfg["unrelated"]
    assert migrated["plugins"]["enabled"] == [NEW]
    assert migrated["plugins"]["disabled"] == ([NEW] if disabled else [])
    assert json.loads(metadata.read_text()) == {"another-plugin": {"pinned": False}}
    assert state.read_bytes() == state_bytes
    assert not old.exists()
    assert receipts.read_bytes() == receipt_bytes
    native_probe(home, other, NEW, enabled=not disabled)
    assert run_documented_step("apply", env).returncode != 0
    assert run_documented_step("rollback", env).returncode == 0
    assert config.read_bytes() == original_config
    assert metadata.read_bytes() == original_metadata
    assert state.read_bytes() == state_bytes
    if disabled:
        assert receipts.read_bytes() == receipt_bytes
    assert old.is_dir() and not (home / "plugins" / NEW).exists()
    native_probe(home, other, OLD, mode="rollback" if not disabled else "verify", enabled=not disabled)
