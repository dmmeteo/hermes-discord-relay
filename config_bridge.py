"""YAML → ``PlatformConfig.extra`` bridge.

The built-in Discord plugin owns the translation of the whole ``discord:``
block in ``config.yaml`` into ``DISCORD_*`` env vars and adapter extras.  This
wrapper runs that translation first — so every existing Discord setting keeps
working untouched — and adds the independent ownership and relay settings this plugin introduces.
Relay is validated before native configuration is applied:

.. code-block:: yaml

    discord:
      thread_ownership_transfer: true          # or the mapping form

Precedence matches the rest of the adapter: an already-set env var wins over
YAML, so ``DISCORD_THREAD_OWNERSHIP_TRANSFER`` works as a kill switch over a
config file that says true.  The adapter enforces that ordering in
``_ownership_config``.

The env mirror is deliberately **not** written here.  ``PlatformConfig.extra``
is per-adapter and therefore profile-safe; process-global env is not, and
under ``gateway.multiplex_profiles`` one profile's value would silently become
another profile's policy (the same hazard the built-in bridge guards with
``_profile_scoped_config_load``).

Failure policy
--------------
Inheriting the built-in bridge is the whole point of this wrapper: without it
``require_mention``, ``auto_thread``, the channel allow/ignore lists, reactions
and backfill — roughly two dozen knobs — stop being applied.  Losing that half
while still applying our one key would be a *partial* config, which is worse
than no plugin at all and impossible to spot from the outside.  So:

* a **missing** built-in bridge is an incompatibility, raised at registration
  time, where the caller declines the override and the stock adapter is used;
* a **raising** built-in bridge is logged at ``ERROR`` and re-raised, leaving
  the gateway in exactly the state a stock Discord install would be in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .compat import IncompatibleHermesError
from .relay import RelayConfig

logger = logging.getLogger(__name__)

CONFIG_KEY = "thread_ownership_transfer"
ENV_FLAG = "DISCORD_THREAD_OWNERSHIP_TRANSFER"


def resolve_builtin_bridge() -> Callable[[dict, dict], dict | None]:
    """Return core's ``_apply_yaml_config``, or raise a named incompatibility.

    Resolved on every call rather than cached at import: the registration path
    calls this once to decide whether to install the override at all, and
    resolving late keeps the check honest under test patching.
    """
    try:
        from plugins.platforms.discord.adapter import (
            _apply_yaml_config as builtin_apply,
        )
    except ImportError as exc:
        raise IncompatibleHermesError(
            "The built-in Discord YAML bridge "
            "(plugins.platforms.discord.adapter._apply_yaml_config) is not "
            "importable; refusing to bridge only part of the discord: block."
        ) from exc

    if not callable(builtin_apply):
        raise IncompatibleHermesError(
            "plugins.platforms.discord.adapter._apply_yaml_config is not "
            "callable on this Hermes build; refusing to bridge only part of "
            "the discord: block."
        )
    return builtin_apply


def _read_ownership_value(yaml_cfg: Any, discord_cfg: Any) -> Any:
    """Find ``thread_ownership_transfer`` in either block shape.

    ``discord_cfg`` is the resolved ``discord:`` block as the gateway passes it
    — which may come from a top-level ``discord:`` key or from
    ``platforms.discord`` / ``gateway.platforms.discord``.  Read it first so
    nested-only configurations work, exactly like the built-in bridge.
    """
    if isinstance(discord_cfg, dict) and CONFIG_KEY in discord_cfg:
        return discord_cfg[CONFIG_KEY]
    if isinstance(yaml_cfg, dict):
        top_level = yaml_cfg.get("discord")
        if isinstance(top_level, dict) and CONFIG_KEY in top_level:
            return top_level[CONFIG_KEY]
    return None


def apply_yaml_config(yaml_cfg: dict, discord_cfg: dict) -> dict:
    """Validate relay, inherit the native bridge, then add the plugin settings."""
    relay_raw = discord_cfg.get("relay") if isinstance(discord_cfg, dict) else None
    if relay_raw is None and isinstance(yaml_cfg.get("discord"), dict):
        relay_raw = yaml_cfg["discord"].get("relay")
    RelayConfig.parse(relay_raw)
    builtin_apply = resolve_builtin_bridge()

    try:
        inherited = builtin_apply(yaml_cfg, discord_cfg)
    except Exception:
        logger.error(
            "The built-in Discord YAML bridge raised. Every discord: setting "
            "it owns (require_mention, auto_thread, allowed/ignored channels, "
            "reactions, backfill, ...) is unapplied. Not applying "
            "%s either — a partial Discord config is worse than none. "
            "Disable this plugin if the problem persists.",
            CONFIG_KEY,
            exc_info=True,
        )
        raise

    extra: dict = {}
    if isinstance(inherited, dict):
        extra.update(inherited)

    if relay_raw is not None:
        extra["relay"] = relay_raw

    raw = _read_ownership_value(yaml_cfg, discord_cfg)
    if raw is None:
        return extra

    # No os.environ write: see the module docstring.  ``extra`` is the
    # authoritative, profile-scoped carrier and the adapter reads it directly.
    extra[CONFIG_KEY] = raw
    return extra
