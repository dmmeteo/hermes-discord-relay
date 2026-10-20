"""Discord Relay, installed as hermes-discord-thread-ownership.

A thin override of the built-in Discord platform adapter.  Everything else —
connection, voice, slash commands, media, auth, reactions, threads, cron
delivery — is inherited from the bundled adapter unchanged.

Registered under platform ID ``discord`` (not a new ID) on purpose: core keys
Discord behaviour off that exact string in ~40 places, including the
``discord`` / ``discord_admin`` toolset restrictions in
``hermes_cli/tools_config.py``.  A separate ID would silently lose all of it.
See README.md for the full rationale.

The override is registered through the supplied plugin context during
:func:`register`. Current Hermes scopes platform registrations to the owning
plugin lifecycle; deferring a later call to ``ctx.register_platform`` escapes
that lifecycle and can publish the entry into the wrong registration scope.

Every failure path ends at the stock adapter.  An incompatible Hermes, a
missing built-in YAML bridge, or an unexpected exception all produce one loud
log line and a working Discord platform without this feature — never a gateway
with no Discord at all.
"""

import logging

from . import compat

logger = logging.getLogger(__name__)

#: Platform ID this plugin registers under.  Overriding the built-in entry is
#: supported by the registry (``PlatformRegistry.register`` is last-writer-wins
#: and drops the bundled deferred loader).
PLATFORM_ID = "discord"

__all__ = ["register", "PLATFORM_ID"]


class _CapturingContext:
    """Collects the built-in adapter's ``register_platform`` keyword args.

    Deliberately tolerant of calls it does not know about.  The built-in
    ``register()`` only calls ``register_platform`` today, but if a future
    Hermes adds, say, ``ctx.register_tool(...)`` there, raising ``AttributeError``
    would take the whole override down.  Unknown calls are recorded in the log
    instead, which is a visible prompt to widen this shim rather than a silent
    downgrade to the stock adapter.
    """

    def __init__(self):
        self.kwargs = None
        self.unexpected_calls: list = []

    def register_platform(self, **kwargs):
        self.kwargs = dict(kwargs)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _recorder(*args, **kwargs):
            self.unexpected_calls.append(name)
            logger.warning(
                "hermes-discord-thread-ownership: the built-in Discord "
                "register() called ctx.%s(), which this plugin does not "
                "forward. That registration is dropped for the Discord "
                "platform; the plugin needs updating.",
                name,
            )

        return _recorder


def _install_override(ctx) -> None:
    """Import the built-in adapter, inherit its entry, and swap two callables.

    The built-in ``register()`` is invoked against a capturing context so this
    plugin inherits every field of its ``PlatformEntry`` — ``check_fn``,
    ``setup_fn``, ``standalone_sender_fn``, ``cron_deliver_env_var``,
    ``max_message_length``, auth env vars, and anything added later — and
    overrides only the adapter factory and the YAML config bridge.

    Raises :class:`compat.IncompatibleHermesError` when this Hermes build does
    not expose the internals the override needs; the caller turns that into a
    fall back to the stock adapter.
    """
    from plugins.platforms.discord.adapter import register as builtin_register

    support = compat.probe()
    compat.require_supported(support)
    if support.missing_optional:
        logger.warning(
            "hermes-discord-thread-ownership: %s. Ownership fallbacks remain "
            "available; relay requires native bot-gate and dispatch hooks. "
            "See README 'Hermes compatibility'.",
            compat.describe(support),
        )

    from .config_bridge import apply_yaml_config, resolve_builtin_bridge

    # Resolve now, not at first config load: a missing built-in bridge must
    # decline the override here rather than silently drop two dozen discord:
    # keys later, deep inside gateway config parsing.
    resolve_builtin_bridge()

    if not compat.ensure_discord_installed():
        # Not fatal: the entry we register inherits the built-in dependency
        # hook, which installs discord.py before the gateway builds an adapter.
        # Which field that is depends on the build — ``ensure_deps_fn`` where
        # upstream split the passive probe from the installer, ``check_fn``
        # before that — and the capturing context forwards every kwarg, so the
        # right one comes across without this plugin having to name it.
        logger.info(
            "hermes-discord-thread-ownership: discord.py is not installed yet; "
            "the inherited dependency hook will install it before first use"
        )

    from .adapter import build_adapter

    captured = _CapturingContext()
    builtin_register(captured)
    if not captured.kwargs:
        raise RuntimeError("built-in Discord adapter registered no platform")

    kwargs = captured.kwargs
    kwargs["name"] = PLATFORM_ID
    kwargs["adapter_factory"] = build_adapter
    kwargs["apply_yaml_config_fn"] = apply_yaml_config
    ctx.register_platform(**kwargs)
    logger.info(
        "hermes-discord-thread-ownership: thread ownership adapter registered "
        "for '%s'",
        PLATFORM_ID,
    )


def _fall_back_to_builtin(ctx) -> None:
    """Register stock Discord when the ownership override cannot load."""
    try:
        from plugins.platforms.discord.adapter import register as builtin_register

        builtin_register(ctx)
        logger.warning(
            "hermes-discord-thread-ownership: override not installed; the "
            "built-in Discord adapter is in use and Discord is unaffected"
        )
    except Exception:
        logger.error(
            "hermes-discord-thread-ownership: override failed and the built-in "
            "Discord adapter could not be registered either",
            exc_info=True,
        )


def register(ctx) -> None:
    """Register the Discord override in the host-owned plugin lifecycle."""
    try:
        _install_override(ctx)
    except compat.IncompatibleHermesError as exc:
        logger.error("hermes-discord-thread-ownership: %s", exc)
        _fall_back_to_builtin(ctx)
    except Exception:
        logger.warning(
            "hermes-discord-thread-ownership failed to install its override",
            exc_info=True,
        )
        _fall_back_to_builtin(ctx)
