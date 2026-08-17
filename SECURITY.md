# Security policy

## Supported versions

Autonomous Intelligence is currently an alpha-stage local vertical slice. Security fixes are applied to the latest version on `main`.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could enable unauthorized filesystem access, approval bypass, replay, secret disclosure, or Broker impersonation.

Use GitHub's private vulnerability reporting for this repository when available. Include:

- Affected commit or version
- Operating system and Python version
- Reproduction steps
- Expected and observed policy behavior
- Whether an unauthorized effect actually occurred
- Suggested mitigation, if known

## Security assumptions

- The MCP host, planner, model output, web content, DOM data, and accessibility metadata are untrusted.
- The execution Broker, approval UI, fixed-rule verifier, and Broker database form the trusted computing base.
- The Broker must run with the least operating-system privilege required by its configured capabilities.
- The writable workspace must not contain Broker state, authentication keys, or approval secrets.

## Current limitations

The current release uses best-effort local file permissions for IPC and approval keys. A production deployment still requires installation-time Windows ACLs, a dedicated service identity, named-pipe access control, and signed release artifacts.
