# Field ownership and adoption belong to reconciliation

Intent fields may declare an explicit withdrawal policy with
`field: {value: X, withdraw: Y}`. `Y` is either a literal value, `adopt`, or
omitted/`null` for no explicit withdrawal. Scalar fields and existing modifier
wrappers remain compatible.

## Decision

The compositor resolves current providers before withdrawal is considered and
exposes per-field provider provenance. Reconciliation persists continuously
owned fields and their withdrawal policy. A disappearing provider therefore
reveals any lower provider first; only a field with no remaining provider is
withdrawn. Simultaneous orphaned fields are sent in one Service plan, with
current resolved values taking precedence.

`withdraw: adopt` captures the normalized actual field when ownership first
becomes continuous, before matching-state dispatch suppression. The captured
value survives provider changes and restart, and is discarded only after the
field becomes unowned. An unavailable or unknown Target cannot be adopted and
dispatch fails closed until a value can be captured.

Modifier-only Intents do not own a baseline. In particular, a cap no longer
creates a value when no provider sets that field. Legacy whole-Target inferred
off remains the fallback for records with no explicit field withdrawal.

Shadow dispatch is a Target policy rather than a second ownership model. It
projects would-apply and would-withdraw decisions but creates no applied-plan,
Drift, or Manual override state.

## Consequences

- Ownership persistence remains in `Reconciliation`, consistent with ADR-0001.
- Adoption records are projection-redacted through the existing recursive
  sensitive-field policy.
- Reload, restart, and simulation use the same provider and withdrawal rules.
- Explicit field withdrawal supersedes legacy whole-Target inferred off.
