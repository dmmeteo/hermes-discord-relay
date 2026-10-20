# Rename the source identity without renaming Discord state

Status: accepted for the local source candidate, 2026-10-21. Publication, native
installer qualification, and live migration remain pending.

## Context

The owner selected Discord Relay and the canonical public repository
[dmmeteo/hermes-discord-relay](https://github.com/dmmeteo/hermes-discord-relay).
The repository rename is a later manager action. Hermes uses the manifest name
for both the plugin install directory and activation key. Changing just the
public copy leaves the old technical identity in every profile. Installing both
identities can register two overrides for platform `discord`.

The existing compatibility promise covers Discord configuration and per-profile
state. Those keys describe behavior and must survive the product rename.

## Decision

Use `hermes-discord-relay` in the source manifest and runtime log prefix. Keep
platform `discord`, all Discord configuration and environment keys, ownership
JSON, and relay receipt SQLite filenames unchanged. Add no runtime alias or
second registration path.

Provide a stopped-writer source migration with an exact activation-key change.
Move the old package outside plugin discovery before placing the new package.
Preserve explicit deny status. Back up original config and installer metadata
bytes. Archive the old installer record and do not invent provenance for the
local clone. Rollback restores the old source and metadata while keeping the
current stores, including receipts admitted after migration.

## Alternatives and consequences

Retaining the former manifest identity would preserve activation but postpone
the requested migration. Replacing strings across all profile files could rename
state paths or corrupt unrelated configuration. A runtime alias would leave two
loadable package identities and complicate lifecycle ownership.

The documented recipe is a manual source candidate, not a native installer
transaction. It rewrites YAML formatting and is not crash-atomic. Its original
config backup and stopped-writer rollback are required. Native installation
calls Hermes PM even when the plugin declares no dependencies, so this task does
not invoke it against the shared Hermes checkout. A pinned disposable native
installer qualification remains required after public rename and before rollout.

## Evidence

`tests/test_identity_migration.py` executes the exact documented apply and
rollback blocks in disposable homes. Native discovery selects the expected
factory, isolates a second profile, restores the built-in on unload, and reloads
the override. The checks cover boolean and mapping config, disabled activation,
unchanged ownership bytes, replay denial through migration and rollback, and
refusal to apply twice. They make no live transport or model-completion claim.
