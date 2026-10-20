# Next epics

The only outcome checklist for this repository. Statuses describe this local candidate. Implemented code is not runtime verification or user acceptance. An item is checked only after its completion condition is verified. Candidates are not selected and are not permission to implement.

## Delivered baseline

This revision implements the opt-in ingress filter described in [README.md](../README.md): single-owner guild threads, owner-only bot handoff with its guards, the per-profile ownership store, the env switch, and the fallback to the built-in adapter when Hermes lacks a required seam. This pass recorded no new runtime or user acceptance.

## Outcomes

- [ ] **Selected: a trusted bot submits once and the recipient reports to the human.**
  - Status: owner-approved scope expansion on 2026-10-20. The local Discord Relay source candidate implements exact directional routes, bounded explicit single-hop envelopes, preserved bot provenance, native authorization, and durable replay receipts independently of ownership. Offline protocol, native adapter/real message, gateway auth, concurrency, restart, profile isolation and lifecycle tests verify source behavior. No live install or guild acceptance is claimed.
  - Done when: in an authorized live guild test with native bot admission `none`, an allowed foreign bot sends one valid request to the correct existing thread, including an already-current owner; the recipient acts and reports to the human; neither bot starts a follow-up conversation. Negative sender, destination, stale, duplicate and forwarded cases produce no agent work across restart.
  - Fails if: a ping alone opens ingress, a relay changes ownership or human identity/approval, global bot trust expands, a repeated request creates another turn, or a send result is called acceptance/completion.
  - Reference: [receiver and sender workflow](../README.md#configure-a-receiver), [relay decision](../docs/adr/0002-controlled-discord-relay.md).

- [ ] **The override takes effect on current Hermes.**
  - Status: source reconciled with public fix `4547de4`. Verified on stock Hermes v0.22.0/Python 3.14.7 through the real loader, registry, profile isolation, unload/reload and existing ownership/admission tests. No live gateway or guild check was run; this outcome remains open. The source-only reconciliation is authorized by the owner assignment of 2026-10-15.
  - Done when: on current Hermes with the feature enabled, the gateway log shows `thread ownership adapter registered`, and in a guild thread only the owner answers an unmentioned human message.
  - Fails if: the feature is enabled but the built-in adapter handles the thread without an `ERROR` that names the cause.
  - Reference: [Hermes compatibility](../README.md#hermes-compatibility).
- [ ] **Candidate, not selected: more than one writer per profile.**
  - Status: unselected. The README says to run one gateway per profile, because two processes deciding on one thread at once are last-writer-wins.
  - Done when: two processes on one profile that decide on the same thread converge on one owner.
  - Fails if: a change makes ownership depend on cross-profile coordination or Discord API calls.
  - Reference: [Remaining constraints](../README.md#remaining-constraints).
