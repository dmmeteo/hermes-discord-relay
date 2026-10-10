# Next epics

The only outcome checklist for this repository. Statuses describe this checkout (local `main`) and say where public `main` differs. Implemented code is not runtime verification or user acceptance. An item is checked only after its completion condition is verified. Candidates are not selected and are not permission to implement.

## Delivered baseline

This revision implements the opt-in ingress filter described in [README.md](../README.md): single-owner guild threads, owner-only bot handoff with its guards, the per-profile ownership store, the env switch, and the fallback to the built-in adapter when Hermes lacks a required seam. This pass recorded no new runtime or user acceptance.

## Outcomes

- [ ] **The override takes effect on current Hermes.**
  - Status: not in this revision. Public `main` registers the override through the plugin context during `register()` (`4547de4`). Here `register()` defers it through `platform_registry.register_deferred`, which that commit says can leave the built-in adapter in use on current Hermes. Bringing the fix here is an owner decision about reconciling this branch.
  - Done when: on current Hermes with the feature enabled, the gateway log shows `thread ownership adapter registered`, and in a guild thread only the owner answers an unmentioned human message.
  - Fails if: the feature is enabled but the built-in adapter handles the thread without an `ERROR` that names the cause.
  - Reference: [Hermes compatibility](../README.md#hermes-compatibility).
- [ ] **Candidate, not selected: more than one writer per profile.**
  - Status: unselected. The README says to run one gateway per profile, because two processes deciding on one thread at once are last-writer-wins.
  - Done when: two processes on one profile that decide on the same thread converge on one owner.
  - Fails if: a change makes ownership depend on cross-profile coordination or Discord API calls.
  - Reference: [Remaining constraints](../README.md#remaining-constraints).
