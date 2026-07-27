# Threat Model

This document describes the security boundaries implemented by the current ctx
codebase. It is not a claim that ctx is a sandbox or a multi-tenant security
service. Vulnerabilities should be reported through the
[SECURITY.md policy](https://github.com/stevesolun/ctx/blob/main/SECURITY.md).

## Scope

The model covers:

- graph, wiki, and external catalog metadata used for recommendations;
- remote and local harness acquisition, installation, and cataloged commands;
- the `ctx-monitor` HTTP boundary;
- local telemetry spools and optional exporters; and
- the transition from an advisory recommendation to operator-approved execution.

The host operating system, LLM provider, external telemetry collector, remote
source repository, and tools launched by a harness remain separate trust
domains. A control in one domain does not make another domain trusted.

## Security Goals

ctx aims to:

1. keep recommendations advisory until a user or host explicitly selects an
   execution path;
2. make remote harness source revisions reproducible by default;
3. avoid passing ambient credentials into cataloged harness commands;
4. keep the monitor local by default and disable remote mutations;
5. keep telemetry local and redacted by default; and
6. preserve enough source, status, and lifecycle evidence for an operator to
   audit a decision.

These goals reduce risk. They do not establish code safety, content safety, or
tenant isolation.

## Trust Boundaries

| Boundary | Data crossing it | Current trust decision |
| --- | --- | --- |
| Catalog/graph to recommender | Names, tags, descriptions, URLs, install commands, status, relationships | Treat as untrusted advisory metadata |
| Remote repository to local install | Git objects and working-tree files | HTTPS and immutable revision required by default; content is still untrusted |
| Cataloged command to host | Executable, arguments, output, filesystem and network activity | Requires explicit command flags; runs with the current user account |
| Browser/client to monitor | Dashboard reads, rendered mutation token, mutation requests | Loopback reads are unauthenticated; exact Origin/Host authority and token checks gate mutations |
| Local process to telemetry spool | Events, metrics, identifiers, failures, token usage | Redacted local files; sanitizer is a data-minimization control |
| Telemetry spool to exporter | Redacted OTLP logs, traces, or metrics | Disabled by default; enabling export creates an intentional data-egress boundary |

## Recommendation Is Not Execution

The graph walk is advisory. Recommendation rows can report availability,
`installable`, `load_status`, and a source reference, but those fields do not
prove that the referenced code is safe. Graph errors are handled as advisory
warnings rather than as permission to execute.

A recommendation does not run an `install_command`, clone a repository, launch
an MCP server, or execute harness setup. Execution begins only through a
separate installer or runtime operation selected by the user or host. The
harness installer then has additional explicit gates:

- local paths and `file://` sources require `--allow-local-source`;
- a remote source without a full commit SHA requires
  `--allow-mutable-repo-head`;
- setup commands require `--approve-commands`; and
- verification commands require `--run-verify`.

Hosts that publish runtime tools remain responsible for applying their own
permission policy. Recommendation quality, availability, and ranking are not
authorization decisions.

## Implemented Controls

### Harness Source Acquisition

Remote harness URLs must use `https://`, contain a host, omit embedded
credentials, and not start with an option-like `-`. Git is launched with
`GIT_ALLOW_PROTOCOL=https`.

URL validation does not classify or block loopback, private, link-local, or
other internal destinations, and it does not resolve or pin DNS before Git
connects. An attacker-controlled catalog `repo_url` can therefore cause clone
or fetch traffic, once an operator invokes installation, to reach
private-network services or a hostname whose address changes after validation.
Requiring HTTPS and a commit SHA protects transport and revision
reproducibility; it does not prevent SSRF or DNS rebinding.

By default, a remote catalog entry must provide a full 40- or 64-character
commit SHA. ctx fetches and checks out that revision in detached mode and
records the resolved commit in the install manifest. The
`--allow-mutable-repo-head` override weakens this guarantee deliberately and
records that mutable `HEAD` was used.

Local sources are disabled by default. When explicitly enabled, ctx rejects a
symlinked source root and any symlink found in that local source tree before
copying it.

Important limitation: the tree-wide symlink check is currently applied to
local sources only. ctx does not perform an equivalent post-check of a remotely
cloned working tree. Pinning proves which revision was fetched; it does not
prove that the revision is benign.

### Harness Command Execution

Cataloged setup and verification commands are split into argument vectors and
executed without a shell. The child environment is rebuilt from a small
allowlist of operating-system and runtime variables. Variables whose names
look like API keys, tokens, passwords, credentials, or other secrets are not
passed through by that allowlist.

Captured command output is truncated and passed through token-shaped and
ambient-secret redaction before it is stored in a result or manifest. Commands
also have a finite subprocess timeout.

ctx does **not** provide process isolation or network isolation. Cataloged
commands run as the current operating-system user, in the materialized harness
directory, with the host's ordinary filesystem permissions and network access.
Environment allowlisting does not stop a process from reading accessible files
or reaching the network. Output redaction does not prevent exfiltration.

### Monitor Boundary

`ctx-monitor` defaults to `127.0.0.1` and is designed as a local operator tool.
On a loopback bind:

- reads are available to loopback clients without authentication;
- mutations require a generated per-process token; and
- browser requests that send an `Origin` header must have the same normalized
  HTTP authority as the request `Host`, including the effective port.

The origin check accepts only `http`, compares the canonical hostname and
effective port, normalizes equivalent IP literals, and rejects malformed
authorities. Originless mutation clients are accepted only when they provide
the valid monitor token.

Loopback mutation pages embed the token in rendered JavaScript so the page can
send authenticated POST requests. Because loopback reads do not require that
token, another local process that can read a mutation page can recover it. The
token is therefore a browser-CSRF and mutation gate, not isolation from another
process running on the same host or account.

Binding to a non-loopback address requires the explicit
`--allow-non-loopback` CLI flag. On that bind, mutation routes are disabled.
Reads require the generated token, supplied directly or through the HttpOnly,
`SameSite=Strict` cookie established by the token URL.

The built-in server does not provide TLS, user identities, RBAC, durable
sessions, or multi-tenant authorization. The printed token URL is a bearer
secret. A non-loopback deployment must not be treated as an internet-facing
enterprise service without a separately managed authenticated TLS boundary.

### Telemetry and Exporters

Telemetry defaults to `local_redacted`. Events and metrics are written to local
JSONL files that ctx creates or tightens to owner-only permissions where the
operating system permits it, and network exporters are disabled by default.
Payload sanitization removes or hashes known raw-input, repository, workspace,
path, command-output, and secret-shaped values. Export paths sanitize records
again before delivery.

Enabling OTLP export creates a new trust boundary. Remote endpoints must use
HTTPS and must be listed in the configured host allowlist. Userinfo, query
strings, fragments, and redirects are rejected. Literal private, reserved,
link-local, multicast, or unspecified remote IP addresses are also rejected.
Plain HTTP is accepted only for loopback collectors.

Exporter headers can carry credentials and the destination collector receives
the exported data. Operators must protect telemetry configuration, the local
spool, hash salts, bearer headers, collector credentials, and the collector
itself. Redaction is best effort and should not be used as permission to place
secrets in telemetry fields. See the [telemetry guide](telemetry.md) for
configuration and retention behavior.

### Graph and Catalog Metadata

Graph, wiki, and catalog content can originate outside the current repository.
The `ctx.core.source_registry` module and `ctx-source-registry` CLI can validate
the built-in registry or an explicitly supplied registry. They record source
revisions, import modes, licenses, and permission evidence, and reject
unapproved full-body import plans submitted to that validator.

The Design.md full-body importer now applies this registry policy, but it is
not a universal ingestion gate. Production graph, wiki, catalog, and
recommendation ingestion paths do not all call it, so content can enter
through a path that bypasses the registry policy. Even when the validator is
used, provenance and licensing do not establish that imported text or code is
trustworthy.

For approved full-body manifests, the registry binds the complete canonical
manifest, including importer-consumed metadata, entry order, source paths, and
body digests. Permission evidence must be a digest-verified checked-in file.
Installed wheels use a packaged copy of those evidence bytes when repository
files are unavailable. Portable source paths reject traversal, ambiguous
Unicode normalization, control/format characters, Windows device names,
alternate-stream syntax, and case-insensitive collisions.
The inherited Design.md corpus remains blocked until it has legal evidence,
body digests, and a registered full-manifest binding.

Metadata can affect ranking, explanations, availability labels, displayed
links, and later operator decisions. Malicious or inaccurate metadata can
therefore cause recommendation poisoning, misleading availability, unsafe
instructions, or prompt injection when content is supplied to an LLM. A local
file check or an `installable=true` result means that material is available,
not that it has passed a security review.

The generic provider loop exposes a raw `ctx__wiki_get` call and result to one
subsequent provider request, then removes both. It rejects blank, duplicate,
or reused tool-call IDs before execution, blocks compaction while raw wiki
context exists, and strips raw wiki results from durable session records and
replay. Text that the model independently quotes or summarizes is ordinary
assistant output and can remain in history; ctx cannot reliably distinguish
that text from a legitimate answer.

Cataloged URLs and commands must remain data until the relevant explicit
installation or execution gate is reached. Consumers should render metadata
as text, preserve provenance, and avoid treating a recommendation score as a
permission grant.

## Residual Risks

| Risk | Why it remains | Current mitigation |
| --- | --- | --- |
| Malicious pinned source | An immutable commit can still contain hostile code | Review the exact revision before approval |
| Remote clone SSRF or DNS rebinding | Harness URL validation accepts internal HTTPS destinations and does not resolve or pin DNS | Restrict egress and permit only reviewed repository hosts |
| Remote repository symlinks | Remote clones do not receive the local-source tree scan | Inspect the checkout and use an external sandbox |
| Host compromise by a command | Commands inherit the user's filesystem rights and network | Do not approve unreviewed commands; use OS isolation outside ctx |
| Recommendation poisoning | Untrusted metadata influences ranking and explanations | Preserve provenance, availability, and operator review |
| Prompt injection from catalog text | Content may be shown to an LLM or human | Treat catalog text as untrusted input, not instructions |
| Model-authored catalog echo | A model can quote or summarize ephemeral wiki content into ordinary assistant history | Minimize requested bodies and treat provider outputs as potentially sensitive |
| Monitor token disclosure | The token URL is a bearer credential and built-in HTTP has no TLS | Keep loopback defaults; use a trusted TLS/auth proxy if exposed |
| Local monitor mutation | An unauthenticated loopback reader can extract the rendered mutation token | Treat local processes as trusted or isolate the monitor by OS identity |
| Telemetry disclosure | Sanitizers can miss novel sensitive values; collectors can be compromised | Minimize payloads, keep export disabled unless required, audit the sink |
| Exporter DNS rebinding | Host allowlisting does not pin or continuously validate a hostname's resolved addresses | Use controlled DNS and an egress-restricted collector path |
| Registry-policy bypass | External ingestion paths are not universally wired through `source_registry` | Run the validator explicitly and review ingestion provenance |
| Same-user local access | Owner-only files do not isolate mutually untrusted processes under one account | Use separate OS identities or hosts for stronger separation |
| Mutable-source override | `HEAD` can change between review and installation | Prefer a full commit SHA and record the resolved revision |

## Operator Baseline

For the lowest-risk supported posture:

1. keep `ctx-monitor` on loopback;
2. leave telemetry network export disabled, or use a loopback collector;
3. allow remote harness network access only to reviewed repository hosts and
   install from an inspected full commit SHA;
4. leave cataloged commands disabled until their exact arguments are reviewed;
5. run `ctx-source-registry` as an explicit pre-ingestion check;
6. do not treat graph or catalog metadata as trusted instructions; and
7. run untrusted tools in an OS-level sandbox, container, VM, or disposable
   account that the operator configures outside ctx.

## Recommended Hardening Not Yet Implemented

The following are recommendations, not current ctx features:

- reject symlinks and other special files after every remote checkout;
- resolve and validate remote repository addresses, reject internal networks,
  pin approved hosts, and enforce outbound network policy before Git connects;
- add a documented process sandbox and deny-by-default network policy for
  executed harnesses and MCP servers;
- wire source-registry validation into every external ingestion entry point
  and reject content without approved provenance;
- add signed source attestations and stronger artifact provenance checks;
- put shared monitor deployments behind enterprise TLS, authentication, and
  authorization; and
- add policy-driven content scanning for catalog metadata before it is supplied
  to a model.

Until those controls exist, deployment policy must account for the residual
risks above.
