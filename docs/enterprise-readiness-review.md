# Enterprise Readiness Review

Status date: 2026-07-26

Repository evidence was reviewed through commit
`8d2a91ef65afb8bec0c8da2169fd174b444da22d`; this report is introduced by the
subsequent documentation commit. GitHub-hosted controls are identified
separately because they are not established by repository files.

This review checks the user-provided enterprise-readiness task packet against
the current repository. It is an evidence record, not a promise that an active
patch or external control has shipped.

## Product Decision

ctx currently supports a local, single-user CLI and monitor. The shared-service
model described in the packet is not the current deployment contract.
Container deployment, OIDC/SSO, multi-user RBAC, and service isolation remain
conditional work until that product model is explicitly adopted.

Enterprise work for the current model prioritizes:

1. private security reporting and repository governance;
2. dependency, static-analysis, and release-supply-chain controls;
3. source and license provenance;
4. local monitor trust boundaries;
5. truthful telemetry and benchmark evidence;
6. reproducible packaging and maintainability.

## Checked Status

| Packet item | Status | Repository evidence | Remaining obligation |
|---|---|---|---|
| P0-1 Security disclosure | Partial | `SECURITY.md`; GitHub private vulnerability reporting enabled | Decide and document response targets, or formally amend that packet criterion; revalidate the repository setting during the release audit |
| P0-2 Independent ownership | External/human blocker | `.github/CODEOWNERS` exists but names only one human owner | Add a second human owner |
| P0-2 Required review controls | Pending operator action | GitHub `main` was unprotected at review time | Enable branch protection, code-owner approval, required checks, and admin enforcement, then validate them with a test PR |
| P0-3 Dependabot, CodeQL, dependency audit | Implemented locally | `.github/dependabot.yml`, CodeQL and dependency-audit workflows, hash-locked policy dependencies, fail-closed Dependabot parser, and security-config classifiers | Pass the final remote workflow matrix and observe the first scheduled Dependabot/audit runs |
| P0-4 License provenance | Critical partial | `src/ctx/core/source_registry.py`, `src/import_designdotmd_skills.py`, generated `NOTICE`, and paired adversarial tests | Extend the enforced registry boundary to every ingestion/recommend/install/publication path; keep unresolved full bodies blocked |
| P1-1 Local monitor trust model | Implemented with accepted substitution | Exact HTTP Origin/Host authority, explicit `--allow-non-loopback`, token-protected reads, disabled remote mutations, tests, and security docs | The packet's separate environment acknowledgement is not implemented; preserve the deliberate flag-plus-read-only-token design or revisit that decision |
| P1-1 Shared-service auth/RBAC | Conditional | No supported shared-service mode | Required before any multi-user or Internet-facing deployment |
| P1-2 Threat model | Implemented | `docs/threat-model.md`, `SECURITY.md`, dashboard guidance, strict-doc tests, and docs navigation | Keep residual risks synchronized as isolation and ingestion controls change |
| P1-2 Execution isolation | Backlog | Existing pinning, path, environment, and redaction controls are not a sandbox | Add process, filesystem, credential, and egress isolation before treating remote code as safely executable |
| P1-3 SBOM and attestations | Implemented locally | Release workflow and validator cover all runtime extras and attest distributions, SBOM, and five graph release assets | Verify attestations against the next real tagged release |
| P1-4 Shared deployment | Conditional | OTLP export already exists for the local product | Docker, readiness, identity, RBAC, and service operations are required only if shared-service scope is adopted |
| P2-1 Package migration | Backlog | `pyproject.toml` still ships legacy flat `py-modules` | Finish migration with compatibility shims and removal dates |
| P2-2 Coverage and typing | Backlog | CI coverage floor remains 40%; mypy still ignores missing imports | Raise the floor in measured steps and tighten core recommend/install paths |
| P2-3 Reproducible install | Partial | The no-test policy gate has a platform-specific hash lock; release artifacts and dependency closure are validated | Add and verify a maintained hash-locked default installation artifact for users |
| P2-4 Oversized modules | Backlog | Several source and test modules remain over the local contribution threshold | Split by responsibility after separate dead-code cleanup commits |

## Review Corrections

The packet is directionally strong, especially on governance, provenance, and
operability. The implementation must make these corrections:

- A declared license or arbitrary permission string is not legal evidence.
  Full-body ingestion must bind immutable evidence and remain blocked when that
  evidence is absent.
- The registry-enforced Design.md import path fails closed, but other catalog,
  recommendation, installation, and publication paths do not yet share that
  enforcement boundary. Policy intent must not be described as universal
  runtime behavior.
- An SBOM generated from only the base wheel environment would be incomplete
  for a product that advertises optional runtimes. The implemented release
  contract therefore covers all six runtime extras and attests graph assets as
  well as distributions and the SBOM.
- A local loopback monitor is not a shared service. A non-loopback opt-in does
  not substitute for identity, RBAC, CSRF controls, isolation, or auditability.
- The local monitor deliberately substitutes one explicit non-loopback flag,
  token-protected reads, and disabled remote mutations for the packet's
  flag-plus-environment-acknowledgement proposal.
- CODEOWNERS without a second human and branch protection does not create
  independent review.
- Functional loopback benchmark evidence must not be labeled as live-provider
  or production-efficiency evidence.
- Dependency automation now uses a fail-closed structural exemption in the
  no-test policy, and the policy gate's own dependencies are hash locked.
  Remote Dependabot and audit execution still needs final-branch evidence.

## Current Provenance Risk

The current catalog audit found a large inherited corpus with sparse license
metadata. The registry-governed Design.md import path responds by failing
closed:

- metadata-only records may remain discoverable with
  `installable=false`;
- unknown-license full bodies are not approved for redistribution or install;
- imported flags never override derived provenance policy;
- historical bodies require quarantine, verified evidence, or human legal
  disposition rather than guessed licenses.

Approved full-body imports now require digest-verified checked-in legal
evidence and a versioned canonical full-manifest binding. The inherited
Design.md corpus remains blocked because it lacks that complete evidence.
The source registry and `NOTICE` are controls and evidence indexes; they do not
constitute legal advice or prove ownership, compatibility, or redistribution
rights.

Other catalog, recommendation, installation, and publication paths can still
derive availability or installability without consulting this registry.
Universal provenance enforcement therefore remains a critical partial item.

## Review Batch Exit Criteria

This repository review batch is complete only when:

1. every active patch above has an independent reviewer verdict;
2. canonical QA trackers match committed code and contain no stale completion
   prose;
3. local-fast, full tests, strict docs, package, security, and browser gates
   pass;
4. remote required checks pass on the final branch;
5. human-owned blockers remain explicitly blocked rather than reported as
   complete;
6. the final no-mistakes run passes after all product phases are finished.

Passing those gates closes this review batch; it does not by itself make ctx
enterprise ready.

## Enterprise-Ready Exit Criteria

An enterprise-ready claim additionally requires:

1. every in-scope P0 and P1 criterion above to be implemented rather than
   partial, pending, or blocked;
2. independent ownership and tested branch-protection controls;
3. universal provenance enforcement across ingestion, recommendation,
   installation, and publication;
4. observed scheduled security-scanner runs and verification against a real
   tagged release; and
5. identity, authorization, and isolation controls before any shared-service
   deployment is supported.
