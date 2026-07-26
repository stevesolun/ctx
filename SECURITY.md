# Security Policy

## Supported Versions

Security fixes target the current `main` branch and the latest published
`claude-ctx` release. Older releases are not actively maintained; reporters
should verify an issue against the latest release or `main` when practical.
ctx requires Python 3.11 or newer.

## Reporting a Vulnerability

Do not open a public issue, discussion, or pull request for a suspected
vulnerability.

Report vulnerabilities only through
[GitHub private vulnerability reporting](https://github.com/stevesolun/ctx/security/advisories/new).
Keep reproduction details, proof-of-concept material, and disclosure
coordination inside that private advisory.

Include:

- the affected version or commit;
- the affected component and environment;
- security impact and realistic attack conditions;
- minimal reproduction steps or a proof of concept;
- any known mitigations; and
- whether the issue is already public.

Do not submit real credentials, tokens, private datasets, or other sensitive
third-party data. Use redacted or synthetic examples.

The maintainer will use the private advisory to validate the report,
coordinate a fix and release, and agree on disclosure. This project does not
currently operate a vulnerability bounty program or promise a fixed response
time.

## In Scope

Reports are welcome for security issues in the shipped Python package and
command-line tools, MCP boundary, recommendation and graph/wiki surfaces,
generic harness runtime, telemetry and exporters, dashboard, installation and
sync paths, and release artifacts. Dependency reports should explain how the
dependency issue is reachable through ctx.

## Current Monitor Trust Boundary

`ctx-monitor` is a local operator tool, not a production multi-tenant service.

- The default `127.0.0.1` bind is full local mode. Dashboard reads are
  available to loopback clients. Mutation endpoints are enabled only in this
  mode and require the generated per-process monitor token. Browser requests
  that supply an `Origin` header must match the exact normalized HTTP
  authority, including the effective port; originless clients are accepted
  only when they provide the valid token.
- A non-loopback bind requires the explicit `--allow-non-loopback` flag and is
  read-only. HTML, JSON API, and server-sent event routes require the generated
  per-process read token (or the HttpOnly, same-site cookie established from
  the token URL). Load, unload, and other POST mutations are disabled.
- Treat the printed token URL as a secret. The built-in server does not turn a
  non-loopback bind into an enterprise authentication or authorization
  service, and it should not be exposed directly to the public internet.

See the [threat model](docs/threat-model.md) for the complete boundary,
implemented controls, and residual risks.

## Governance Status

This policy does not claim that Dependabot, CodeQL, branch protection, or
mandatory multi-person approval is currently enabled. Those controls are
managed separately and do not replace private vulnerability reporting.
