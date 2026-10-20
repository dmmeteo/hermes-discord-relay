# Keep controlled relay separate from thread ownership

Status: accepted for local source implementation, 2026-10-20. Live guild
acceptance and public publication remain pending.

## Context

A trusted foreign bot must submit work to an already-current owner with native
`DISCORD_ALLOW_BOTS=none`. The existing owner-only handoff marker cannot express
this request. It transfers ownership and requires the sender to own the thread.
An arbitrary bot mention must not open an agent turn.

The owner explicitly authorizes a narrow expansion of the owner-only
force-admission boundary for trusted relay. Install identity, off-by-default
configuration, platform ID, human authorization, and no-core-edit boundaries
remain in place. The plugin still makes no Discord API calls. Sender workflows
reuse Hermes' existing Discord tools.

## Decision

Keep `discord.relay` independent from `discord.thread_ownership_transfer`.
Use exact directional sender/guild/thread allow and deny routes, with deny
precedence. Admit only a direct, bounded, single-hop v1 envelope. Validate native
bot identity, destination, age, expiry, channel gates, and native sender policy.
Reject self, webhook, reply, forwarded, nested, malformed, and duplicate input.

Use SQLite admission receipts independent of the ownership JSON store. Commit
a per-recipient request reservation and a unique message reservation before returning admission. Bound the
store and refuse new requests at capacity. Fail closed on storage errors. Each
transaction opens and closes its connection, so reload has no receipt writer
or persistent handle to drain. Capture its path at adapter construction in the
owning profile scope.

Use the native per-adapter bot hook for one synchronous admission call. Relay
requires that hook and the native recovered-dispatch keyword. It never changes
the global bot environment. The recovered path prevents native text coalescing
from combining independently receipted requests. A copied message adds provenance
and recipient instructions while preserving the original message and bot author.
No role, upstream relay, or human authorization flag is granted. Hermes still
checks authorization downstream. This Discord feature is distinct from Hermes'
authenticated upstream relay transport.

No reliable bounded native admission acknowledgement was found. Do not send an
acknowledgement or equate transport success with agent admission or completion.
The sender sends once and stops. The recipient reports to the human. Behavioral
instructions do not sandbox model tools.

## Alternatives

Reusing the ownership reducer and asynchronous JSON writer would couple a relay
to an ownership transition, lose receipts on a crash, and retain multi-writer
races. It cannot satisfy this feature's invariants.

A new bot client or model-generated receipt would add transport ownership,
credentials, and reply-loop risk without proving completion. Existing native
send capability and truthful unconfirmed status are sufficient.

## Consequences and evidence

Admission is at most once, not exactly-once work. A crash after receipt commit
can lose a request before dispatch. Synchronous SQLite commits can pause ingress.
Retention is 24 hours and capacity is 4,096, with no eviction of live receipts.
Sender IDs still need native authorization. Neither payload claims nor relay
routes authenticate a human or an approval.

The incident regression fails on the previous source and passes with relay.
Protocol, native adapter, real Discord message, gateway auth, receipt concurrency,
restart, profile isolation, and native lifecycle tests cover the source behavior.
Live Discord delivery, agent completion, sender adherence, and human acceptance
remain unexercised. See the sole [outcome checklist](../../planning/next-epics.md).
