# Decisions

- [Register the override in the plugin lifecycle](0001-plugin-scoped-registration.md). Why the plugin delegates registration ownership to Hermes instead of adding its own deferred loader.
- [Keep controlled relay separate from ownership](0002-controlled-discord-relay.md). Exact trusted routes, single-hop envelopes, durable receipts, and truthful delivery status.
- [Rename the source identity without renaming Discord state](0003-discord-relay-identity.md). Canonical manifest, exact activation-key migration, preserved stores, and honest installer provenance.
