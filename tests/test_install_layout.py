"""Install layout, discovery, and the opt-in gate.

There is no installer in this repository — ``hermes plugins install`` clones the
repo straight into ``~/.hermes/plugins/<manifest name>/`` and imports that
directory.  So the thing worth testing is not a script but a *shape*: that the
repository root is a valid plugin directory for the installer this machine has,
and that the real ``PluginManager`` finds, gates, and loads it from there.

These tests copy the repository the same way the installer moves the clone, then
drive the actual discovery chain — no mocks of the loader, the registry, or the
config reader.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from conftest import PLUGIN_DIR

MANIFEST = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))
PLUGIN_NAME = MANIFEST["name"]

#: The canonical GitHub slug this plugin is published under.  It is what every
#: documented ``hermes plugins install`` command must say, and its repository
#: half must equal the manifest name — that is the rule ``plugin.yaml`` states
#: and the reason a reader's first command works.  The *local* checkout
#: directory is irrelevant to both (the installer derives the install path from
#: the manifest, never from the clone's directory name), so it is deliberately
#: not asserted on.
REPO_SLUG = "dmmeteo/hermes-discord-thread-ownership"

README = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")

#: Everything the installed copy needs at runtime.  Anything the package
#: imports must be here, or discovery fails with an ImportError.
RUNTIME_MODULES = ("__init__.py", "adapter.py", "compat.py", "config_bridge.py",
                   "ownership.py")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "plugins").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _install(home: Path) -> Path:
    """Reproduce what ``hermes plugins install`` does with a fresh clone.

    ``_install_plugin_core`` clones to a temp dir and then
    ``shutil.move``s the whole tree to ``<plugins>/<manifest name>``.  Copying
    the repository wholesale is the faithful stand-in — in particular it means
    ``tests/`` lands in the installed directory too, so if that ever broke
    discovery these tests would catch it.
    """
    target = home / "plugins" / PLUGIN_NAME
    shutil.copytree(PLUGIN_DIR, target, ignore=shutil.ignore_patterns(
        ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
    ))
    return target


def _write_config(home: Path, **sections) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(sections), encoding="utf-8")


def _fresh_discovery():
    """Run plugin discovery against a clean registry and return it."""
    from gateway.platform_registry import platform_registry
    from hermes_cli.plugins import PluginManager

    platform_registry._entries.clear()
    platform_registry._deferred.clear()
    PluginManager().discover_and_load(force=True)
    return platform_registry


def _entry_module(entry) -> str:
    return getattr(entry.adapter_factory, "__module__", "")


# ── the shape the installer expects ─────────────────────────────────────────


class TestInstallerContract:
    """Pinned against the installer in the Hermes source on this machine."""

    def test_the_repository_root_is_the_plugin_directory(self):
        assert (PLUGIN_DIR / "plugin.yaml").is_file()
        assert (PLUGIN_DIR / "__init__.py").is_file()

    def test_the_installer_would_accept_this_directory(self):
        """``_install_plugin_core`` warns unless one of these is present."""
        from hermes_cli.plugins_cmd import _read_manifest

        assert _read_manifest(PLUGIN_DIR)["name"] == PLUGIN_NAME

    def test_the_manifest_version_is_one_the_installer_supports(self):
        from hermes_cli.plugins_cmd import PluginOperationError, _check_manifest_version

        _check_manifest_version(MANIFEST, PLUGIN_NAME)
        with pytest.raises(PluginOperationError, match="manifest_version"):
            _check_manifest_version({**MANIFEST, "manifest_version": 9999}, PLUGIN_NAME)

    def test_the_install_directory_name_comes_from_the_manifest(self, tmp_path):
        """A name the sanitiser rejects would fail the install outright."""
        from hermes_cli.plugins_cmd import _sanitize_plugin_name

        assert _sanitize_plugin_name(PLUGIN_NAME, tmp_path).name == PLUGIN_NAME

    def test_the_repo_slug_and_the_manifest_name_are_the_same_string(self):
        """``plugin.yaml`` says "keep it identical to the repository name"."""
        owner, sep, repo = REPO_SLUG.partition("/")
        assert sep == "/" and owner
        assert repo == PLUGIN_NAME

    def test_every_documented_install_command_uses_the_canonical_slug(self):
        """A wrong slug here is a 404 on the reader's very first command."""
        installs = re.findall(r"plugins\s+install\s+(\S+)", README)
        assert installs, "the README must document the install command"
        assert set(installs) == {REPO_SLUG}

    def test_every_other_documented_plugin_command_uses_the_manifest_name(self):
        """``enable``/``disable``/``update``/``remove`` match on the manifest name."""
        commands = re.findall(
            r"plugins\s+(?:enable|disable|update|remove)\s+(\S+)", README
        )
        assert commands
        assert set(commands) == {PLUGIN_NAME}

    def test_the_manifest_declares_the_platform_kind(self):
        assert MANIFEST["kind"] == "platform"

    def test_the_manifest_carries_no_field_the_loader_drops(self):
        """A key ``PluginManifest`` has no field for is dead config."""
        import dataclasses

        from hermes_cli.plugins import PluginManifest

        known = {f.name for f in dataclasses.fields(PluginManifest)}
        known.add("manifest_version")   # read by the installer, not the manifest
        assert set(MANIFEST) <= known

    def test_the_author_is_not_misattributed(self):
        assert MANIFEST["author"] == "dmmeteo"

    def test_no_install_script_ships_with_the_plugin(self):
        """The official CLI does this job, profile-aware, for free."""
        assert not (PLUGIN_DIR / "install.sh").exists()

    def test_every_runtime_module_is_present(self):
        for name in RUNTIME_MODULES:
            assert (PLUGIN_DIR / name).is_file(), name


# ── discovery from an installed copy ────────────────────────────────────────


class TestDiscovery:
    def test_the_installed_copy_has_every_runtime_module(self, hermes_home):
        target = _install(hermes_home)
        for name in RUNTIME_MODULES:
            assert (target / name).is_file(), name

    def test_installed_but_not_enabled_keeps_the_builtin(self, hermes_home):
        _install(hermes_home)
        _write_config(hermes_home, plugins={"enabled": []})
        registry = _fresh_discovery()
        entry = registry.get("discord")
        assert entry is not None
        assert PLUGIN_NAME.replace("-", "_") not in _entry_module(entry)

    def test_enabling_it_overrides_the_discord_entry(self, hermes_home):
        _install(hermes_home)
        _write_config(hermes_home, plugins={"enabled": [PLUGIN_NAME]})
        registry = _fresh_discovery()
        entry = registry.get("discord")
        assert PLUGIN_NAME.replace("-", "_") in _entry_module(entry)

    def test_the_overriding_entry_builds_the_custom_adapter(self, hermes_home):
        from gateway.config import PlatformConfig

        _install(hermes_home)
        _write_config(hermes_home, plugins={"enabled": [PLUGIN_NAME]})
        registry = _fresh_discovery()
        cfg = PlatformConfig(enabled=True, token="t")
        cfg.extra = {}
        adapter = registry.get("discord").adapter_factory(cfg)
        assert type(adapter).__name__ == "DiscordCustomAdapter"

    def test_built_in_credentials_survive_the_override(self, hermes_home):
        _install(hermes_home)
        _write_config(hermes_home, plugins={"enabled": [PLUGIN_NAME]})
        registry = _fresh_discovery()
        entry = registry.get("discord")
        assert entry.required_env == ["DISCORD_BOT_TOKEN"]
        assert entry.cron_deliver_env_var == "DISCORD_HOME_CHANNEL"

    def test_disabling_it_restores_the_builtin(self, hermes_home):
        _install(hermes_home)
        _write_config(
            hermes_home,
            plugins={"enabled": [PLUGIN_NAME], "disabled": [PLUGIN_NAME]},
        )
        registry = _fresh_discovery()
        assert PLUGIN_NAME.replace("-", "_") not in _entry_module(registry.get("discord"))

    def test_no_install_means_no_trace_of_the_plugin(self, hermes_home):
        _write_config(hermes_home, plugins={"enabled": [PLUGIN_NAME]})
        registry = _fresh_discovery()
        assert PLUGIN_NAME.replace("-", "_") not in _entry_module(registry.get("discord"))

    def test_the_module_is_imported_under_the_expected_slug(self, hermes_home):
        """The loader slug is what ``conftest`` reproduces for every test."""
        _install(hermes_home)
        _write_config(hermes_home, plugins={"enabled": [PLUGIN_NAME]})
        _fresh_discovery()
        assert f"hermes_plugins.{PLUGIN_NAME.replace('-', '_')}" in sys.modules

    def test_the_bundled_discord_plugin_is_untouched(self):
        from hermes_cli.plugins import get_bundled_plugins_dir

        bundled = get_bundled_plugins_dir() / "platforms" / "discord"
        assert (bundled / "plugin.yaml").exists()
        assert bundled.resolve() not in PLUGIN_DIR.parents


# ── the toolset contract the platform ID buys us ────────────────────────────


class TestPlatformIdContract:
    def test_native_discord_toolsets_stay_reachable(self):
        """Why the platform ID is "discord" and not a new one.

        ``_TOOLSET_PLATFORM_RESTRICTIONS`` pins the ``discord`` and
        ``discord_admin`` toolsets to the ``discord`` platform, so any other ID
        would silently lose them.
        """
        from hermes_cli.tools_config import _toolset_allowed_for_platform

        for toolset in ("discord", "discord_admin"):
            assert _toolset_allowed_for_platform(toolset, "discord")

    def test_platform_enum_resolves_the_id(self):
        from gateway.config import Platform

        assert Platform("discord") is Platform.DISCORD
