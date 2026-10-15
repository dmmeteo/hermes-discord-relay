# Register the override in the plugin lifecycle

Accepted for the local source candidate on 2026-10-15. No deployment is implied.

## Established evidence

The earlier source and README defer registration to avoid importing the Discord
adapter during eager plugin discovery. Public commit `4547de4` replaces that
loader with registration through the supplied context. The installed read-only
reference has the same registration code as that public commit.

On stock Hermes v0.22.0, `PluginContext.register_platform()` publishes into the
manager's profile scope and records an undo operation. The bundled Discord
loader is also scoped. The old plugin checks only the global entry map and
publishes its own global loader. That loader loses to the scoped built-in entry.
The cold-process regression reproduces this with both deferred and materialized
built-ins, despite Hermes reporting the plugin enabled.

## Decision and tradeoff

Call `_install_override(ctx)` during `register()`, retaining the existing
compatibility checks and built-in fallback. Hermes owns registration, profile
isolation, load deadlines and teardown. A custom scoped deferred loader would
have to participate in those same lifecycle rules and adds no needed behavior.

The adapter module can now load during plugin discovery. No startup latency
claim is made. `discord.py` remains optional until adapter construction through
the inherited dependency hook. Platform ID, configuration, ingress rules,
bot-handoff guards and ownership persistence do not change.

`tests/test_lifecycle.py` exercises the stock manager, adapter selection, a second
profile, unload, reload and restoration of the displaced built-in registration.
The outcome checklist keeps live guild acceptance separate from these checks.
