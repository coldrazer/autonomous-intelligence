# Implementation status

## v0.3 MCP vertical slice

| Contract | Status | Evidence |
|---|---|---|
| Distinct logical and attempt identities | Implemented | `ActionRequest` and journal schemas |
| Broker-authoritative dispatch ledger | Implemented | `BrokerLedger` and `ExecutionBroker` |
| Idempotent request submission | Implemented | Same-ID/same-payload and mutation tests |
| Durable Engine/Broker reconciliation | Implemented | Crash failpoints at acceptance, pre-effect, and post-effect boundaries |
| Semantic action classification | Implemented for files | Broker policy derives `PURE` and `RECONCILABLE` |
| Exact approval binding | Implemented for file writes | Single-use expiring HMAC approval record |
| Capability-scoped paths | Implemented | Canonical parent resolution and deny paths |
| Named-pipe process separation | Implemented on Windows | Authenticated raw JSON transport test |
| MCP v2 stdio adapter | Implemented | Official SDK in-memory and subprocess protocol tests |
| Typed MCP tool schemas | Implemented | Five constrained tools with descriptions and annotations |
| MCP error signaling | Implemented | Correctable failures produce `is_error=true` tool results |
| MCP resources | Implemented | `autonomous-intelligence://capabilities` |
| Host-neutral client configuration | Implemented | Codex, Claude, Kimi, Antigravity, Gemini, Cursor, VS Code, and generic renderers |
| Windows UI Automation | Not yet implemented | Requires a trusted target resolver and input-contention guard |
| Browser CDP adapter | Not yet implemented | Must bind targets to browser profile, process, origin, frame, and structural revision |
| LLM goal planner | Supplied by MCP host | Server accepts only typed semantic tool calls |

## Recommended next milestones

1. Add a read-only Windows UI Automation observation adapter.
2. Add structural fingerprints and ambiguity rejection without permitting physical input.
3. Implement `set_checkbox(desired_state)` with human-input contamination detection.
4. Run the crash matrix in a disposable Windows VM.
5. Add a CDP browser adapter with origin/frame binding.
6. Let MCP hosts use each new adapter only after it passes adversarial acceptance tests.

## Security boundary

The planner is assumed compromised. It may propose any supported request, reuse identifiers, mutate parameters, or target hostile paths. The broker independently canonicalizes requests, assigns action classes, obtains approval, commits dispatch state, and performs the semantic action.

The current IPC authentication secret and approval key are stored with best-effort local file permissions. A production Windows service must replace this with installation-time ACLs, service identities, named-pipe access control, and signed broker binaries.
