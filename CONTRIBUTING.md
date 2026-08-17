# Contributing to Autonomous Intelligence

Thank you for helping build safer local agent execution.

## Design constraints

Contributions must preserve these invariants:

1. The planner and MCP adapter are untrusted.
2. The Broker derives policy and action class independently.
3. Dispatch state is committed before crossing the side-effect boundary.
4. Non-idempotent uncertain effects are never retried automatically.
5. New actions have typed preconditions, postconditions, and recovery behavior.
6. A Broker failure never causes a direct-execution fallback.

## Development setup

```powershell
git clone https://github.com/coldrazer/autonomous-intelligence.git
cd autonomous-intelligence
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

## Pull requests

- Keep changes narrowly scoped.
- Add tests for success, denial, replay, and crash recovery where applicable.
- Document any new MCP tool or resource in the README.
- Do not weaken workspace containment or approval requirements to simplify a feature.
- Explain the action class and uncertain-state behavior for every new side effect.

## Commit style

Use short imperative subjects, for example:

```text
Add reconciliable checkbox action
Reject ambiguous UI fingerprints
Test Broker crash before delivery
```

By contributing, you agree that your contribution is licensed under the MIT License.
