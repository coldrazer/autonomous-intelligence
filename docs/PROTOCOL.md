# Protocol contract

The MCP adapter is an untrusted client of this protocol. MCP transport success never bypasses Broker policy, approval, or journal recovery.

## Identifiers

- `logical_operation_id`: stable business intent across retries.
- `attempt_id`: unique dispatch attempt and broker replay key.
- `external_idempotency_key`: stable provider key when an adapter supports it.
- `approval_id`: single-use broker authorization record for one exact attempt.

## Submission idempotency

The broker canonicalizes the request after resolving paths and applying policy.

- Unknown `attempt_id`: record `ACCEPTED` durably.
- Known ID with the same canonical payload: return or resume its current state.
- Known ID with a different canonical payload: reject as a replay conflict.

## Dispatch ordering

```text
Engine PREPARED commit
  → Broker ACCEPTED commit
  → Approval issued and consumed when required
  → Broker IN_FLIGHT commit
  → Perform semantic effect
  → Broker DELIVERY_ATTEMPTED commit
  → Engine observes typed postcondition
  → Engine VERIFIED commit
```

`IN_FLIGHT` is intentionally conservative. A crash immediately before the effect and a crash immediately after the effect are indistinguishable until reconciliation.

## File action contracts

### `read_file`

Inputs: workspace-contained regular-file path.

Class: `PURE`.

Postcondition: the broker returns UTF-8 content and its SHA-256 digest.

### `write_file_atomic`

Inputs: workspace-contained path, UTF-8 content, expected content hash, optional expected prior hash, and explicit overwrite flag.

Class: `RECONCILABLE`.

Precondition for recovery retry: the target is absent, or its current hash equals `expected_prior_hash`.

Postcondition: the target hash equals `expected_hash`.

Execution: write and flush a temporary file in the target directory, then replace the target atomically.
