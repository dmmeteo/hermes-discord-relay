"""hermes-discord-thread-ownership — single-owner Discord thread admission.

A thin override of the built-in Discord platform adapter.  Everything else —
connection, voice, slash commands, media, auth, reactions, threads, cron
delivery — is inherited from the bundled adapter unchanged.

Registered under platform ID ``discord`` (not a new ID) on purpose: core keys
Discord behaviour off that exact string in ~40 places, including the
``discord`` / ``discord_admin`` toolset restrictions in
``hermes_cli/tools_config.py``.  A separate ID would silently lose all of it.
See README.md for the full rationale.

Loading is deferred through the platform registry, not merely into
:func:`register`.  User platform plugins are loaded *eagerly* during plugin
discovery, and discovery runs from ``model_tools`` on essentially every
``hermes`` invocation — so importing the Discord adapter from ``register()``
would add ~1-2s to plain ``hermes chat``, which is exactly the cost the bundled
deferred-loader design exists to avoid.  Instead :func:`register` only installs
a loader; the built-in adapter is imported the first time something actually
asks the registry for the Discord platform.

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
            "hermes-discord-thread-ownership: %s. The feature is fully "
            "available; these paths are less profile-scoped than the native "
            "ones. See README 'Hermes compatibility'.",
            compat.describe(support),
        )

    from .config_bridge import apply_yaml_config, resolve_builtin_bridge

    # Resolve now, not at first config load: a missing built-in bridge must
    # decline the override here rather than silently drop two dozen discord:
    # keys later, deep inside gateway config parsing.
    resolve_builtin_bridge()

    if not compat.ensure_discord_installed():
        # Not fatal: the entry we register inherits the built-in ``check_fn``,
        # which installs discord.py before the gateway builds an adapter.
        logger.info(
            "hermes-discord-thread-ownership: discord.py is not installed yet; "
            "the inherited check_fn will install it before first use"
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
    """Last resort: register the stock Discord adapter under its own name.

    Reached only if :func:`_install_override` raised *after* our loader had
    already displaced the bundled one, where doing nothing would leave the
    gateway with no Discord platform at all.
    """
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
    """Install a deferred loader that overrides the Discord platform entry.

    Nothing heavy is imported here.  ``register()`` runs during plugin
    discovery, which happens on nearly every ``hermes`` invocation; the actual
    override runs on first registry lookup of the Discord platform (gateway
    start, cron delivery, ``hermes setup``, ``send_message``).

    Ordering is favourable: bundled platform plugins are scanned before user
    plugins, so our ``register_deferred`` replaces the bundled Discord loader.
    If the bundled entry was already materialised concretely, we override it
    eagerly instead — ``register_deferred`` would be a no-op in that case.
    """
    try:
        from gateway.platform_registry import platform_registry

        def _loader() -> None:
            try:
                _install_override(ctx)
            except compat.IncompatibleHermesError as exc:
                # A version mismatch is an operator-actionable condition, not a
                # transient glitch: say so at ERROR, once, with the exact list
                # of missing symbols.
                logger.error("hermes-discord-thread-ownership: %s", exc)
                _fall_back_to_builtin(ctx)
            except Exception:
                logger.warning(
                    "hermes-discord-thread-ownership failed to install its "
                    "override",
                    exc_info=True,
                )
                _fall_back_to_builtin(ctx)

        # "Is there already a *concrete* entry?" — deliberately not
        # ``get()``/``is_registered()``: the former would fire the bundled
        # deferred loader (the import we are avoiding), the latter counts a
        # pending loader as registered.  No public accessor draws that
        # distinction, so read the map defensively and treat any surprise as
        # "nothing concrete yet".
        concrete = getattr(platform_registry, "_entries", {}).get(PLATFORM_ID)

        if concrete is not None:
            _loader()
        else:
            platform_registry.register_deferred(PLATFORM_ID, _loader)
            logger.debug(
                "hermes-discord-thread-ownership: deferred override registered "
                "for '%s'",
                PLATFORM_ID,
            )
    except Exception:
        logger.warning(
            "hermes-discord-thread-ownership failed to register; "
            "falling back to the built-in Discord adapter",
            exc_info=True,
        )
