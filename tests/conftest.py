"""Standalone test harness.

These tests run outside the Hermes tree, against whatever Hermes source the
machine actually has installed.  Two things follow from that:

* The Hermes source root has to go on ``sys.path`` before anything imports
  ``plugins.platforms.discord.adapter``.  It is resolved from
  ``HERMES_SOURCE_ROOT`` when set, else ``$HERMES_HOME/hermes-agent``, else
  ``~/.hermes/hermes-agent`` — the layout the official installer produces.
* The plugin is imported exactly the way ``PluginManager._load_directory_module``
  imports it at runtime: as ``hermes_plugins.<slug>`` built from a file-location
  spec with ``submodule_search_locations``.  Importing it any other way would
  test a package shape that never exists in production.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

#: Repository root — which is also the plugin package directory, because the
#: Hermes installer moves the whole clone to ``~/.hermes/plugins/<name>/``.
PLUGIN_DIR = Path(__file__).resolve().parents[1]

#: Mirrors ``hermes_cli.plugins._NS_PARENT`` and the manifest-name slug rule
#: (``key.replace("/", "__").replace("-", "_")``).
NS_PARENT = "hermes_plugins"
PLUGIN_SLUG = "hermes_discord_thread_ownership"
PKG_NAME = f"{NS_PARENT}.{PLUGIN_SLUG}"


def hermes_source_root() -> Path:
    """Locate the Hermes checkout these tests run against."""
    explicit = os.environ.get("HERMES_SOURCE_ROOT")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        candidates.append(Path(hermes_home) / "hermes-agent")
    candidates.append(Path.home() / ".hermes" / "hermes-agent")

    for candidate in candidates:
        if (candidate / "plugins" / "platforms" / "discord" / "adapter.py").is_file():
            return candidate.resolve()

    pytest.exit(
        "Hermes source not found. Set HERMES_SOURCE_ROOT to a Hermes checkout "
        f"(tried: {', '.join(str(c) for c in candidates)}).",
        returncode=4,
    )


HERMES_ROOT = hermes_source_root()
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))


def load_plugin_package() -> types.ModuleType:
    """Import the plugin the way the Hermes directory loader does."""
    if PKG_NAME in sys.modules:
        return sys.modules[PKG_NAME]

    if NS_PARENT not in sys.modules:
        ns_pkg = types.ModuleType(NS_PARENT)
        ns_pkg.__path__ = []  # type: ignore[attr-defined]
        ns_pkg.__package__ = NS_PARENT
        sys.modules[NS_PARENT] = ns_pkg

    spec = importlib.util.spec_from_file_location(
        PKG_NAME,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = PKG_NAME
    module.__path__ = [str(PLUGIN_DIR)]  # type: ignore[attr-defined]
    sys.modules[PKG_NAME] = module
    spec.loader.exec_module(module)
    return module


def load_plugin_module(submodule: str) -> types.ModuleType:
    """Import one submodule of the plugin package."""
    load_plugin_package()
    return importlib.import_module(f"{PKG_NAME}.{submodule}")


@pytest.fixture
def plugin():
    return load_plugin_package()


@pytest.fixture
def registry():
    """The real platform registry, restored after each test."""
    from gateway.platform_registry import platform_registry

    entries = dict(platform_registry._entries)
    deferred = dict(platform_registry._deferred)
    try:
        yield platform_registry
    finally:
        platform_registry._entries = entries
        platform_registry._deferred = deferred


@pytest.fixture
def plugin_context():
    """A real ``PluginContext`` bound to this plugin's manifest."""

    def _make(name: str = "hermes-discord-thread-ownership", source: str = "user"):
        from unittest.mock import MagicMock

        from hermes_cli.plugins import PluginContext, PluginManifest

        manifest = PluginManifest(
            name=name,
            version="1.0.0",
            description="test",
            author="dmmeteo",
            path=str(PLUGIN_DIR),
            source=source,
            kind="platform",
        )
        manager = MagicMock()
        manager._plugin_platform_names = set()
        return PluginContext(manifest, manager)

    return _make
