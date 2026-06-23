# Adapter package uses sibling functions + shared registry, not unified domain handlers

We split `src/intentional/ha_adapter.py` into a flat `adapter/` package of sibling modules (`translator`, `matcher`, `extractor`, `signer`, `registry`). We deliberately did **not** collapse the three per-domain ladders into a unified domain-handler table that owns all translation directions per domain.

## Considered options

- **U — Unified domain-handler table.** One descriptor per domain owning set→calls, calls→expected-state, and state→set. Rejected: the three ladders have lopsided complexity (the translator is ~750 lines; the matcher and extractor are ~110 each), so unifying makes `media_player`'s handler a giant next to a two-line `switch` handler, and collapsing the ladders is a big-bang rewrite of the most-tested code in the repo (~3000 lines of translator tests) with no leverage gain over the registry.
- **S — Sibling functions + shared domain/field registry** (chosen). The functions stay separate and consult a shared registry of domain/field metadata. Lower risk, incremental, reversible, and the registry is the prerequisite for U anyway — you can't unify handlers per-domain until the per-domain data is in one place.

## Consequences

- Candidate 3 (the field registry) is absorbed into this candidate: the registry module IS the single source of truth that kills the four-way `MANUAL_SET_FIELDS` duplication, the compositor's `COLOR_FIELDS`, and the inline `FIRE_SERVICE_SCHEMA`.
- U remains a possible later move if per-domain duplication still hurts after the registry lands. The sibling shape doesn't block it.
- The state extractor (`manual_set_from_state_object`) lives in the adapter package even though its only production consumer is the Reconciliation module's drift classifier — domain knowledge consolidates with domain knowledge regardless of current consumer count.
- No protocol/seam is introduced: there is only one concrete implementation of HA domain translation, so the package is pure functions co-locating domain knowledge, not a ports-and-adapters adapter despite the name.
