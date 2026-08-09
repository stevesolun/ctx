# Unified CTX Capability Engine

This is the internal execution plan for replacing CTX's fragmented recommendation
and lifecycle paths with one host-neutral capability engine. It is intentionally
kept under `docs/plans/`, outside the public documentation navigation.

## Goal

Build one engine that observes the work currently being performed and maintains
the smallest useful set of CTX capabilities for that work. The engine may
recommend or activate zero to five skills, agents, MCP servers, or harnesses,
then cool or deactivate them as the task changes. It must work through Codex,
Claude Code, `ctx run`, MCP, LoopFlow, and imported Python harnesses without
pretending those hosts expose identical controls.

The product rule is:

> Keep capabilities in the catalog or installed workshop when useful later;
> place only the capabilities needed now into the active belt; distinguish
> exposure from actual use; remove capabilities from the active belt before
> proposing persistent uninstall.

When a relevant capability is absent, the selected knowledge graph may still
offer it as installable. For skills, agents, and MCP servers, each user chooses
one of two persistent-install consent routes: preapprove automatic installation
under a typed policy, or require a fresh confirmation for each exact install.
Both routes are consent; neither permits execution of free-form graph prose as
an install command.

CTX setup must capture and persist that choice independently for skills,
agents, and MCP servers. A user may choose automatic installation for one kind
and ask-each-time for another. Until an explicit choice is persisted, the safe
default for every kind is ask-each-time. Changing the policy creates a new
immutable policy snapshot; it does not retroactively authorize an already
pending action.

## Non-goals

- Do not make the engine own an LLM/provider loop or host UI.
- Do not use MCP itself as the lifecycle authority.
- Do not rebuild the graph corpus as part of the engine refactor.
- Do not implement a remote organization service before local value is proven.
- Do not install outside the user's configured preapproval policy or an exact
  ask-each-time grant, and never silently uninstall persistent capabilities.
- Do not delete current APIs, CLIs, hooks, state files, or package shims before
  compatibility, rollback, and release gates pass.

## Product-first refactor rule

The engine is the only source of truth for recommendation, lifecycle, consent,
budgets, and evidence. Existing scorers, state stores, adapters, and CLIs are
inputs or compatibility facades; none may remain a second policy engine.

Refactoring is preferred when it removes duplicated decisions, false usage
signals, per-host behavior drift, or an abstraction that cannot represent the
tool-belt lifecycle. Compatibility work is rejected when it would compromise:

- a useful globally bounded zero-to-five recommendation set;
- current-work relevance and calibrated abstention;
- honest evidence of exposure, invocation, utility, token cost, and elapsed
  time;
- one consistent result across Codex, Claude Code, and the CTX-owned loop;
- the ability to deactivate context without conflating it with uninstall.

The first product milestone is a complete, measurable loop for Codex and Claude
Code: observe current work, produce a small cross-type set, explain why each
item helps now, expose only accepted items, and record enough evidence to
compare cost and outcome with a no-CTX control.

## Architecture decision

Use one revisioned, event-sourced engine with multiple thin host adapters.

The stable public interface is intentionally small:

```python
class CtxEngine:
    def process(self, event: EngineEvent) -> Transition: ...
    def snapshot(self, scope: ScopeRef) -> EngineSnapshot: ...
```

`CtxEngine` owns recommendation policy, lifecycle state, consent rules, active
budgets, desired-set reconciliation, staleness policy, and audit history. It
returns typed host actions but never performs host mutations itself.

Each adapter has two responsibilities:

1. Translate native host facts into engine events.
2. Present or execute returned actions and send receipts back.

`TurnController` remains the adapter seam for the CTX-owned generic loop.
`CtxCoreToolbox` and the MCP server remain compatibility/transport facades.

## Domain language

These are distinct facts and must never be collapsed:

| Dimension | States | Meaning |
| --- | --- | --- |
| Selection | unseen, proposed, accepted, rejected, deferred, expired | Whether CTX considered and offered a capability |
| Installation | unknown, absent, present | Whether the capability exists persistently in the host/workspace |
| Activation | unknown, inactive, active | Whether a verified lease currently owns host context or tools |
| Exposure | unexposed, prepared, submitted | Whether capability content or schemas reached a provider request |
| Invocation | not-invoked, invoked-failed, invoked-succeeded | Whether a capability was actually called and with what result |
| Utility | unobserved, opportunity-seen, effective, validated, idle, harmful | Whether stronger evidence supports or contradicts usefulness |
| Consent | not-required, pending, granted, denied, expired | Whether one exact action is authorized |

`deactivate` releases runtime context or tools. `uninstall` removes persistent
host state. `archive` changes catalog quality status. They are separate actions.

## Scope and budgets

Every event is scoped by tenant, workspace, repository, current-work session,
exposure, and host context. Exposures retain separate attribution for parent and
child agents, but the user-visible recommendation bundle and the union of active
CTX additions are each globally bounded to zero through five unique
capabilities across the current-work session. A child exposure cannot create a
second five-capability belt. Immutable baseline host capabilities do not consume
the CTX count, but they do count when detecting duplicate coverage.

The entity count is not sufficient by itself. Policy also enforces:

- context byte and estimated-token budgets;
- tool-schema and selected-tool budgets;
- active MCP process budgets;
- child-agent and harness budgets;
- permissions and credential requirements;
- transitive dependency cost;
- workspace-wide shared-resource limits.

An MCP exposing an oversized tool catalog must support a relevant subset or be
reported as manual/non-actionable.

## Protocol

Every external event carries:

- protocol version;
- unique event ID;
- complete scope;
- expected revision;
- occurrence time;
- typed payload;
- correlation and causation IDs;
- privacy/retention label;
- engine, protocol, planner, policy, and host-descriptor versions/digests;
- catalog, semantic model, and semantic index snapshot digests;
- the persisted normalized work signature and any deterministic random seed.

Initial event kinds:

- `SessionStarted`
- `WorkspaceObserved`
- `IntentObserved`
- `DevelopmentObserved`
- `TurnStarting`
- `ProviderSubmissionObserved`
- `ToolCallObserved`
- `ValidationObserved`
- `UserDecision`
- `ActionApplied`
- `ActionFailed`
- `ActionExpired`
- `ReassessmentRequested`
- `TurnEnded`
- `SessionEnded`

Initial action kinds:

- `PresentBundle`
- `RequestConsent`
- `InstallCapability`
- `ActivateCapability`
- `PrepareExposure`
- `DeactivateCapability`
- `UninstallCapability`
- `Notify`
- `NoChange`

Every physical action has a deterministic action ID, entity and source digest,
plan and catalog snapshot IDs, precondition revision, lease, expiry, required
host feature, verification requirement, and rollback metadata.

A newly accepted event at revision `N` is committed as exactly `N + 1` before
actions are delivered. Returned actions bind their precondition to committed
revision `N + 1`. Action receipts echo the action ID, kind, engine-issued
content digest, and action precondition revision; applied, failed, and expired
receipts then add verification, structured error, or expiry reason evidence.
Exact duplicate events return the originally cached transition and do not
create a synthetic no-op revision.

Wire values use the engine-owned `ctx-canonical-json-v1` representation.
Adapters submit structured values and echo engine-issued digests; they do not
reimplement hashing. Duplicate object keys, unknown envelope fields, nonfinite
numbers, non-string object keys, and unpaired Unicode surrogates are rejected.
Timestamps use strict RFC 3339 input and are normalized to UTC `Z` form before
hashing.

`WorkspaceObserved`, `IntentObserved`, `DevelopmentObserved`, `TurnStarting`,
`ValidationObserved`, and `ReassessmentRequested` are decision-causing events.
They must carry the complete frozen replay metadata and deterministic seed.
Evidence, receipt, and session-boundary events replay already-frozen facts and
may omit decision metadata. A `UserDecision` binds grant or denial to one
consent ID and exact persistent action ID, kind, digest, and precondition
revision for the next commit; a free-form or reusable consent token is invalid.
For example, a consent request committed at revision `N + 1` binds the exact
persistent action precomputed for the decision transition at `N + 2`. Any
intervening committed event makes the approval stale and requires a new request.

The engine journals normalized planner inputs and the decided plan. Replay never
re-reads volatile raw content or re-runs a later planner/index version to
reconstruct an earlier decision. A volatile reference that cannot produce a
persistable normalized observation is ineligible for a replayable decision.
Raw prompts, code, diffs, secrets, tool output, and absolute paths do not enter
the authoritative journal. Ingress content is collision-bound by digest while
the journal stores a privacy-approved, replay-complete structured surrogate. If
that surrogate cannot be produced, the event fails before reduction or storage.

The default replay normalizer is closed, not heuristic. It currently accepts
only the reducer's exact structured session, desired-set, provider-submission,
tool-outcome, consent, receipt, turn-boundary, and session-boundary schemas.
Free-form workspace, intent, development, and validation observations remain
unsupported until their typed analyzers produce an approved versioned
surrogate. Rejection happens before journal access. Replay consumes only stored
surrogates and an exact reducer version; it never calls an analyzer, planner,
catalog, filesystem, clock, network, host, or random source.

## Evidence semantics

Opportunity and value evidence are typed by capability kind:

- A **skill** is exposed when its exact content digest reaches a provider
  request. Exposure alone is not use. Stronger evidence requires an attributable
  downstream action, validation change, user confirmation, or other host-visible
  signal tied to the skill's declared guidance.
- An **agent** is invoked when a matching child run starts, succeeds only when
  that run completes, and becomes effective only when its output is consumed by
  the parent/work product or receives attributable validation/user evidence.
- An **MCP server/tool** is exposed when selected schemas reach the provider,
  invoked on an exact tool call, succeeded only on a successful result, and
  effective only when a downstream action consumes that result or validation or
  user evidence attributes value to it.
- A **harness** is active for a verified execution span. It becomes effective
  only through attributable orchestration, validation, or outcome evidence.

An opportunity window is capability-specific and exists only when the current
work matches its declared need and the adapter can observe the relevant facts.
Evidence fixtures must cover positive, missing, ambiguous, failed, and
counter-evidence cases for every capability kind before the reducer can classify
`idle`, `effective`, `validated`, or `harmful`.

## Non-negotiable invariants

1. A request never implies application. Only a matching host receipt changes
   physical installation or activation state.
2. Provider submission records exposure, never use or usefulness.
3. A tool attempt records invocation. Success and usefulness require separate
   evidence.
4. Persistent installation requires either a matching user-preapproved policy
   for that capability kind and typed plan or a fresh action-specific grant.
   Uninstall, permission expansion, and credential changes always require an
   exact action-specific grant.
5. Unsupported host actions remain deferred/manual and never become applied.
6. The same journaled normalized observations, decided plans, catalog/index and
   policy snapshots, host descriptor, engine/planner versions, and seed reproduce
   the same state and actions.
7. Duplicate event replay is idempotent; same ID with different content fails
   as a collision.
8. Journal commit precedes action delivery. Retry returns the same action ID.
9. Store failure emits no host mutation. Planner failure retains the current
   active set and emits a degraded diagnostic.
10. Multiple leases may share a physical activation; only the final owner may
    cause physical deactivation.
11. Parent and child agents have separate exposures and usage attribution.
12. Absence of evidence is usable only when the host declares the relevant
    opportunity observable.
13. Idle classification requires rolling observed opportunity, grace, and
    hysteresis. Task change alone cannot imply persistent uninstall.
14. Activation threshold exceeds retention threshold. Marginal score changes
    must not cause capability churn.
15. Return zero when no candidate clears a calibrated relevance and
    actionability threshold.
16. At most one semantic-equivalence representative appears unless two entries
    have explicit complementary roles.
17. Replacement cannot transiently create a sixth active capability. Prepare or
    install the replacement while inactive, then use an atomic host swap when
    supported; otherwise deactivate the old capability before activation and
    reactivate it from the verified rollback plan if the replacement fails.
18. The engine limits prompt frequency as well as capability count.

## Host capability levels

Adapters publish a versioned descriptor of observations, actions, evidence,
UI, and resource limits they support. Capability levels are derived from that
descriptor:

- **Query-only:** explain recommendations and accept rejection feedback.
- **Observing:** submit structured repository/task/development events.
- **Activating:** apply and revoke ephemeral leases with receipts.
- **Managing:** perform persistent install/uninstall with consent and rollback.

The adapter level must be visible in engine snapshots and benchmark evidence.
A weak host must not infer stale context from events it cannot observe.

## Recommendation plan

The current graph scorer remains a retrieval primitive. The unified planner
combines structured current work, baseline and active capabilities, rejection
history, host features, catalog policy, and a frozen catalog snapshot.

Candidate retrieval may use lexical, semantic, and graph signals. A set-level
ranker then maximizes unmet-need coverage and complementarity while penalizing
duplication, incompatibility, context/runtime cost, permissions, and risk.

Every actionable transition includes the typed plan, verification, and rollback
required for that exact transition. Unsupported transitions are reported
separately. A host may therefore activate a preinstalled capability without also
supporting its installation or uninstall. Query-only hosts may still present
useful manual recommendations. Installed-but-inactive capabilities are offered
as activation, not installation.

Absent capabilities remain eligible when the CTX catalog, active user graph, or
active organization graph contains an authenticated, typed install descriptor.
The graph source and descriptor provenance are bound to the frozen catalog
snapshot, so a similarity edge or description cannot substitute for installer
authority. Policy preserves the planner's committed order and applies the
user's per-kind route:
`preapproved-auto` or `ask-each-time`. Unsupported or unverified install plans
remain advisory; a similarity score never authorizes a raw command.

Both routes use one auditable reducer path. The engine always emits an exact
`RequestConsent` bound to a precomputed `InstallCapability` action. In
`ask-each-time` mode the host presents that request; in `preapproved-auto` mode
the consent dispatcher immediately records a policy-backed grant without UI.
The same exact `UserDecision`, install action, verification receipt, and
rollback contract follow in either case. Default policy is `ask-each-time`
independently for skills, agents, and MCP servers. A policy-backed grant is
valid only for the matching authenticated policy, capability kind, source,
catalog, installer identity, plan digest, and target scope. Permission
expansion, credential requirements, uninstall, and any changed plan always need
a fresh interactive grant.

Schema-v3 planning, consent, receipts, and installed-material lineage support
skills, agents, and MCP servers through one exact contract. Physical drivers
remain independently gated per kind. The first persistent actuator may still
be deliberately narrow: atomic installation of an authenticated skill into an
allowed local root, with required static scanning, post-copy digest
verification, and reversible promotion. Agent and MCP plans remain
non-executable until their drivers satisfy the same one-use authorization,
verification, and rollback contract. Legacy graph `install_command` values and
model-visible commands never enter an engine plan.

## Cooling and deactivation

Task and subtask changes trigger reassessment, not immediate removal.

- Each task, subtask, and agent owns a lease.
- Capabilities enter `cooling` before deactivation unless a task/session ends or
  the user explicitly rejects them.
- A replacement needs a configurable utility improvement sustained across
  meaningful observations or an explicit task boundary.
- Deactivated capabilities receive a task-scoped cooldown before re-offer.
- Context/runtime deactivation may be automatic only when the user separately
  preapproved automatic deactivation for that capability kind; installation
  preapproval never implies deactivation or unload preapproval. The safe
  default is `ask-each-time` independently for skills, agents, and MCP servers.
- Mandatory process/session resource cleanup is not persistent deactivation.
  Persistent uninstall always remains a separately authenticated user proposal
  and is never inferred from cooling.
- Returning to a previous subtask should resume an installed capability without
  reinstalling or repeating broad consent.

Numeric thresholds remain policy values until longitudinal evidence supports
defaults. The ordering and hysteresis rules are fixed invariants.

## Privacy and trust

Persist structured facts and digests by default. Raw prompts, code, diffs, tool
outputs, and absolute paths remain host-owned through volatile references unless
the user explicitly selects a different policy.

Every event, action, and receipt carries privacy and retention labels. The
engine rejects unsanitizable persistent payloads. Recommendation actionability
requires source provenance, trust policy, platform compatibility, and security
status. Telemetry remains opt-in and aggregate.

## Module boundary

```text
src/ctx/engine/
  __init__.py       stable exports only
  capability_schema.py shared closed plan and host-context limits
  benefit.py        authenticated value facts and bounded net-benefit selection
  benefit_audit_store.py append-only digest-addressed selection evidence
  content.py        exact material descriptors and authorization
  installation.py   typed plans, per-kind policy, and consent presentation
  lineage.py        catalog and installed-material identity transitions
  observation.py    current-work observation contracts
  planning_v3.py    authenticated capability-plan schema v3
  protocol.py       versioned events, actions, transitions, receipts
  state.py          snapshots and orthogonal capability facts
  reducer.py        pure deterministic transition rules
  replay.py         privacy-safe durable observations and decisions
  store.py          authoritative SQLite journal and rebuildable projections
  engine.py         process/snapshot coordinator
  planner.py        legacy contracts plus shared bounded candidate types

src/ctx/runtime/
  _query_attempt_posix.py fixed-stripe descriptor-relative query scratch pool
  planning_v3.py    frozen-input adapter for authenticated planning
  composition.py    one production construction and lifetime boundary
  query_session.py  closed new-session query decision for host adapters
  query_decision.py sealed host-neutral query receipt and context rendering
  query_delivery.py durable one-use Codex/Claude hook issuance boundary

src/ctx/adapters/generic/
  engine_turn.py    one-use lower-authority committed-bundle presentation

src/ctx/adapters/
  query_hook_io.py  bounded fail-soft native hook input/output
  claude_code/engine_hook.py pure Claude hook projection
  claude_code/query_handler.py executable Claude prompt handler
  codex/query_hook.py pure Codex hook projection
  codex/hook_handler.py executable Codex prompt handler

remaining target surfaces:

src/ctx/analyzers/
  repository.py
  diagnostics.py
  languages/

src/ctx/adapters/
  claude_code/install_query_hook.py
  codex/install_query_hook.py
  mcp/engine_tools.py
```

Current import paths remain adapters that call inward. Adapters must not import
each other.

## Compatibility envelope

Preserve during migration when doing so does not create a second source of
truth. These are facade-level contracts, not a requirement to retain their
current internals:

- top-level `ctx` and `ctx.api` exports and error behavior;
- `CtxCoreToolbox` constructor, tool names, schemas, dispatch shape, rejection
  memory, restrictions, and JSON results;
- `ctx-recommend`, `ctx-scan-repo`, dashboard, and monitor routes;
- MCP JSON-RPC framing and existing tool catalogue;
- `ctx run` commands, session JSONL, resume semantics, tool surfaces, output,
  and exit codes;
- LoopFlow V1 payload and permissions;
- Claude settings merge, hook commands, installers, and legacy state readers;
- configuration merge behavior and existing keys;
- console scripts, wheel data, flat shims, and old import paths;
- benchmark compatibility until a new treatment protocol is frozen.

A global all-type plan is an additive contract first. Legacy
`recommend_bundle()` continues returning its current row projection while a new
versioned planning API exposes harnesses and action plans.

## Rollout modes

The engine supports a global mode plus per-surface overrides:

- `legacy`: old implementation only;
- `shadow`: new engine observes and journals; old result wins;
- `recommend`: new recommendations are visible; no automatic effects;
- `activate`: ephemeral effects are allowed with receipts;
- `manage`: persistent effects are enabled with exact consent.

An emergency environment override forces `legacy`. The resolved mode is stored
with resumable sessions so restart cannot silently change semantics.

## Task graph

Execution status on 2026-08-02:

Authoritative latest checkpoint (later than the historical milestone notes
retained below):

- The current release path is schema-v3 planning plus receipt-bound physical
  actions. Release sequence 4 keeps the exact 750-byte
  `skill:ctx-python-testing` load canary and gives
  `skill:ctx-python-state-protocols` two separately reviewed realizations: an
  absent `install` row with a typed plan and an installed `load` row with the
  exact 823-byte material descriptor. The installed body is no longer present
  in the packaged load asset: one engine-issued, process-bound bundle permit
  routes each selected realization either to its authenticated package source
  or directly to its verified CAS source. The host-policy availability snapshot
  may expose only one realization for an identity; exposing both fails before
  benefit closure or planning. The install-only asset is first read after the
  durable exact driver claim. Query receipts bind the exact action, work
  signature, host invocation, content digest, and final journal revision. Raw
  bodies remain absent from the journal, audit store, and delivery ledger.
- Codex and Claude Code share the host-neutral delivery controller and emit the
  same canonical context through thin host envelopes. Their explicit
  installers now use bounded, locked, no-follow configuration updates and
  preserve current official handler variants. Independent POSIX security
  review found no remaining P0/P1 across malformed configuration, symlink and
  hardlink attacks, live lock/parent replacement, exact size bounds, and
  thread/process contention.
- A fresh wheel installs both entry points and reproduces the exact canary
  bytes through both fake host journeys without importing from the checkout.
  The clean-wheel contract, focused package/handler suites, Ruff, formatting,
  and targeted mypy are green. Actual Claude/Codex invocation and trust UI,
  model consumption, and native Windows execution remain unverified release
  gates.
- The old unified benchmark treatment remains dry-run-only historical wiring
  and is not the current product-proof path. The strict loopback deterministic
  provider bridge and its current revision-three integration are now green.
  The approval-bound pair proves serial arms, distinct application paths,
  identical normalized environments and workspace identities, an empty model
  tool surface, the exact single prompt delta, exact provider request/response
  bytes, and reproducible token accounting. The final direct-script regression
  validates that only the reviewed sibling bridge module is imported. Focused
  bridge/pair tests, compatibility tests, static checks, and independent
  security and methodology reviews pass.
- That deterministic pair is deliberately claim-ineligible: it has no OS-level
  sibling-filesystem isolation, uses a constant-output provider, and both arms
  fail the evaluator. It therefore proves transport and accounting, not CTX
  benefit. The latest retained report is
  `.gate/ctx-ab-runs/deterministic-bridge-root-check-v4/deterministic-bridge-report.json`;
  its hard gates keep product, production-efficiency, and benefit claims false.
  The next live gate is one user-authorized same-repository/task/model/tools/
  budget pilot pair; it is a demonstration, not a general product claim, and no
  cost-bearing provider run is authorized yet. A publishable benefit campaign
  remains later than sufficient multi-capability/catalog coverage and requires
  its own preregistered multi-repository design and cost approval. The first
  installed-skill later-turn path is now implemented for both Codex and Claude
  Code: an active exact CAS capability re-enters the same global planner as a
  reviewed load realization and is routed only after the complete prompt bundle
  receives journal-backed one-shot authority. Agents/MCPs, opportunity-aware unload
  prompting, layered user/organization graphs, real-host invocation, and native
  Windows remain product slices before that broader claim. Persistent uninstall
  remains separately authorized.
- The host-neutral release-skill dispatcher now closes the core absent-skill
  seam: safe-default ask, explicitly persisted per-kind auto, authenticated
  interactive grant/deny, policy/expiry checks, secure POSIX CAS execution,
  exact receipt, installed lineage, and pending activation eligibility. It is
  idempotent and alias-stable for the same real workspace. Independent security
  review and a fresh isolated wheel are green. The installed-skill activation
  actuator is now also accepted: a dedicated additive engine/store claim binds
  the exact pending action, verifier target, installed lineage, host descriptor,
  and release root before CAS inspection; an actual-time durable outcome then
  settles the exact receipt atomically. Generic applied, failed, expired, and
  rollback activation receipts fail closed without that authority. Retries
  reverify exact CAS bytes and preserve the original outcome time; legacy active
  journals remain readable without fabricating missing authority. The public
  dispatcher now exposes only the activation action digest. Independent final
  review found no remaining P0-P2 after 139 related tests and repeated
  concurrency stress. The subsequent exposure bridge is read-only and
  final-head linearized: it rederives activation settlement and exact CAS bytes
  under the material lock through a strict read-only journal store, accepts only
  an engine-issued per-selection route from the authorized bundle,
  and returns a process-bound one-shot body. Codex and Claude Code now use that
  bridge on later relevant turns; irrelevant turns abstain, and activation-state
  changes advance the invocation/receipt identity while the durable delivery
  slot remains epoch-independent and at-most-once for one logical prompt. A new
  logical prompt is reassessed. Availability is opened exactly once under that
  prompt slot and is never allowed to activate an inactive capability. Agents
  can reuse the dispatcher afterward; MCP requires a trusted actuator.
- This vertical path is still scoped by host and native session. A skill
  installed in one native session is not yet discoverable from a later native
  session or the other host; the workspace-scoped capability inventory remains
  an open blocker before this becomes the generic skills/agents/MCP foundation.
  Claude Code's documented hook payload also lacks a native turn identifier, so
  same-text submissions in one Claude session conservatively share one logical
  prompt terminal; Codex uses its real turn identifier. Full turn-parity is not
  claimed.
- The current release-sequence-4 worktree passes all 19 local PR-preflight
  lanes: 7,723 tests passed with nine skips and 92.03 percent coverage;
  repository-wide mypy checked 526 source files; Ruff, formatting, dependency,
  policy, isolated-wheel Claude/Codex host execution, public tracker, strict
  docs, telemetry, similarity, browser-security, reproducible-build, and
  package checks all passed. The reproducible wheel digest is
  `23e7f1e9af3bdd18546326850e35c4b2db88d00e76aff02b3a383d6052c2d7cb` and the
  sdist digest is
  `23387ca900a23061e83fd2b99337fb0ff3a6b16c5d65f3b49c336c90e020c640`.
  Independent integrated review reports no remaining P0 or P1 finding in this
  slice. Native Windows and real interactive Codex/Claude executions remain
  separate evidence gates. No cost-bearing provider campaign was run.

The following bullets preserve the evidence trail of earlier milestones. When
they describe a component as pending or abstention-only, the authoritative
checkpoint above governs current status.

- Live re-baseline: committed query receipts now bind the exact presentation
  action ID and content digest, normalized-work signature, and host invocation.
  A shared Codex/Claude hook-delivery controller and both executable
  `UserPromptSubmit` handlers are implemented locally. Focused handler/delivery
  tests and static checks pass, but the latest retention repair is still under
  final independent acceptance review. Its transient query journals now use
  eight fixed POSIX scratch/lock stripes, stream bounded recovery input, purge
  authenticated managed residue before terminal commit, recover crash residue
  without clocks, and leave zero clean-call attempt children. Pre-write
  ancestry checks reject rename-exposed state roots; supported macOS system
  aliases are canonicalized through the shared secure opener. Secure installers,
  real-host smokes, native Windows execution, and the wider current-worktree
  gates remain open.
- Stages 1 and 3 and the core of Stage 2 are implemented and independently
  accepted. Stage 5 remains active for production analyzers, multilingual
  relevance evaluation, holdout evidence, and latency hardening.
- The controlled, no-provider-token Stage 4 treatment remains useful wiring,
  attribution, relevance, and bounded-overhead evidence for two canaries. It is
  not product-benefit proof and must not be represented as such.
- Capability-plan schema v3 is now integrated end to end through replay, state,
  reducer, coordinator, runtime planning, durable benefit audit, and production
  composition. Legacy v1/v2 serialization and behavior remain frozen.
- One frozen planning-environment digest binds policy, calibration, benefit
  facts, catalog namespace and retrieval snapshot, material snapshot, install
  snapshot, and planner version. Exact typed load authority binds the retrieved
  presentation to catalog identity and material; exact install authority binds
  descriptor v2 to its full result material.
- The deterministic selector enforces one global cross-type zero-to-five set,
  admits only positive marginal net benefit, and can stop before five or
  abstain. Active and new capabilities compete inside the same budget.
- Durable benefit audit stores canonical bounded full results privately by
  digest while replay/state retain only the compact reference. Initialization,
  concurrent writers, corruption, oversized values, exact-byte idempotency, and
  transient secure-lock creation were independently stress-reviewed.
- Planning, consent, receipt, activation, and install-to-load lineage now cover
  skills, agents, and MCP servers. Cross-kind, catalog, descriptor, material,
  requested-action, and policy substitutions fail closed. Manual rows remain
  advisory and never acquire activation leases.
- The host-neutral consent dispatcher consumes the actual reducer-produced v3
  request. Independent per-kind `preapproved-auto` and `ask-each-time` policy is
  persisted; permission expansion or credential requirements always force an
  interactive decision. Both routes still traverse the same journaled action
  and receipt path.
- Candidate-local malformed authority is skipped without manual downgrade and
  without discarding valid peers. Frozen snapshot drift remains global. An
  independently reviewed all-pairs matrix covers repeated and bidirectional
  transitions among typed failure, absence, malformed, valid, and changed
  authority outputs.
- Composition constructs SQLite benefit audit, authenticated net-benefit
  planner, schema-v3 replay planner, reducer-v3 replay factory, journal,
  persisted-policy authority, exact descriptor loader, trusted clock, and
  optional host-authenticated decision guard. It now also owns an optional
  trusted physical-driver registry rather than exposing raw driver construction
  through the public runtime package.
- Stage 7 now includes the append-only one-use execution-claim ledger, durable
  execution outcomes, atomic receipt settlement, a process-bound
  non-serializable one-shot execution handle, kind-aware driver routing, and
  two concrete POSIX actuators: an owner-private skill content store and an
  owner-private inactive agent workshop. Both bind the exact host, installer,
  target directory identity, descriptor, and result material; load content
  only after a new durable claim; publish without replacement; durably verify
  exact bytes; and reconcile crash stages without reapplying. The agent format
  additionally permits only exact string `name` and `description` frontmatter,
  rejects ambiguous YAML and executable metadata, and uses a fixed-size
  recovery namespace. All child operations are descriptor-relative to pinned
  directories. File or directory durability failure remains indeterminate and
  cannot settle an applied receipt. Windows fails closed before filesystem
  access until native DACL/reparse/no-overwrite/durability actuators exist.
  Independent adversarial review found no remaining P0, P1, or P2 finding
  within the documented cooperative same-user CTX runtime boundary. Agent host
  projection/activation and the MCP physical driver remain independently gated.
- The last full current-worktree PR preflight, run before the current executable
  host-handler/delivery slice, passed all 19 local lanes: 7,415 unit tests passed with five skips and
  92.15 percent coverage; repository-wide mypy covered 493 source files; Ruff,
  formatting, policy, clean-host installation, strict docs, similarity, browser
  security, reproducible build, and package checks all passed. The repaired
  focused query slice passes 202 tests, its broader selected gate passes 1,377,
  and independent integrity review found no P0, P1, P2, or P3 issue. The
  committed-HEAD local-fast gate is not represented as current-worktree
  evidence because it correctly refuses this uncommitted implementation. These
  results are historical regression evidence, not verification of the newer
  query-delivery and handler changes.
- Per-kind onboarding policy was implemented ahead of Stage 7. Automatic host
  dispatch, agent projection/activation, the MCP physical driver, native
  Windows installation, verified host rollback, and real-host evidence remain
  pending.
- Current graph consumption uses one authenticated frozen artifact. Local,
  user, and organization graph layering, precedence, and merged provenance
  remain Stage 8 work.
- Claude Code and Codex now have policy-free projections and executable
  fail-soft `UserPromptSubmit` handlers over the same sealed,
  host-neutral query decision used by `ctx run`. Both target the verified
  pre-work `UserPromptSubmit` envelope and preserve the exact semantic
  presentation digest while retaining separate host and journal receipts.
  A keyed digest-only ledger burns one logical-prompt delivery slot before a
  process-bound one-shot emission permit is returned. Repeated prompt IDs remain
  at-most-once while later prompt IDs can be reassessed. Legacy one-slot sessions
  fail closed after upgrade, and an atomic 65,536-row ceiling bounds durable
  digest-only interaction metadata. The public controller
  pins the production engine and renders the sealed host envelope directly;
  test substitution is private. Concurrent permit use, installation-key replacement/loss,
  pre-existing SQLite sidecars, conflicting terminal replay, insecure roots,
  workspace aliases, crash-after-quarantine recovery, and a crash after terminal
  commit but before permit construction now have focused regressions. If cleanup
  fails, the observable result is `failed` even when a durable terminal already
  exists; the terminal remains burned, planning does not repeat, and a later
  successful cleanup returns `already-terminal`. The digest-only ledger is an
  issuance authority, not reconstructable recommendation-quality or product-
  benefit evidence. Optional WAL/SHM disappearance during concurrent SQLite
  authentication is treated as normal absence, while surviving unsafe sidecars
  still fail closed; the repaired concurrent crash storm passed 60 consecutive
  repetitions locally. Secure installer/config wiring, latency calibration,
  feedback receipts, real-host proof, native Windows cleanup support/evidence,
  MCP, and LoopFlow migration remain pending.
- Execution-order correction: now that the first two bounded local actuators
  are accepted, pause additional actuator expansion. The query-only `ctx run`
  slice now opens the release-pinned catalog, precomputes and journals one
  normalized current-work decision before entering the provider loop, and
  gives a journal-write-free `EngineTurnController` the exact committed bundle
  for one lower-authority presentation. Codex and Claude Code now reuse that
  exact conformance value in pure projections. The current product slice is
  hardening the executable one-use hook handlers and delivery boundary; secure
  installation and real-host delivery evidence follow before MCP registration.
- The live host-frontier re-baseline found four prerequisites for that slice:
  a production-owned frozen catalog provenance object, a concrete authenticated
  benefit-facts source plus reviewed policy/calibration, a one-use digest-bound
  observation registry that never journals raw prompt/code/path data, and a
  closed schema-v3 recommendation projection. The query facade exposes only
  process/snapshot/close behavior and rejects every transition except an
  abstention or exactly one `PresentBundle`; it has no exposure, install,
  activation, content-loading, or receipt surface.
- The prerequisite contracts now implemented and independently accepted are:
  the one-use observation registry, closed schema-v3 renderer, catalog-bound
  query-work normalizer, bounded authenticated vocabulary, exact-presentation
  benefit-facts loader, shared bounded candidate normalization, and the
  query-scoped reviewed-benefit closure. The last contract preserves separate
  CTX, organization, and user authorities; binds exact catalog material,
  presentation, policy, and current-host facts; supports abstention; and is
  factory-sealed against reconstructed permission or provenance claims. Three
  independent final reviews found no remaining P0, P1, or P2 issue, and the
  latest surrounding gate passes 196 tests while the complete runtime suite
  passes 255 tests with Ruff, formatting, mypy, and diff checks green. These
  are accepted prerequisites, not by themselves a production integration.
- The next release-pinned catalog slice is now implemented and independently
  accepted. A no-argument factory owns the code-pinned release-root digest,
  loads four exact package resources, joins each catalog layer one-to-one with
  its independently reviewed benefit authority, applies exact current-host and
  policy gates before the 512-candidate closure, and carries separate
  per-authority executable provenance plus one aggregate composition pin.
  Equivalent rows may collapse only under the same policy-visible interaction
  signature; a still-distinct feasible frontier above 512 fails closed instead
  of silently dropping a useful capability. Prepared queries and the release
  facade are factory-sealed, close revokes their authority views, and host
  callbacks cannot deadlock release close. Packaging and Windows-focused CI now
  pin the assets and open the release factory from an isolated wheel. The
  shipped catalog intentionally contains no positive reviewed entries and is
  therefore `abstention-only`; real positive reviewed profiles, the upstream
  authenticated semantic retrieval index, and richer current-host facts remain
  required before useful user-visible recommendations.
  Independent correctness, security, and packaging reviews found no remaining
  P0, P1, or P2 issue on the final frozen hashes. The current broad focused gate
  passes 1,303 tests; the final runtime review passes 279 tests; an isolated
  wheel contains all four resources and opens the factory successfully. The
  root is SHA-pinned rather than signed and has no cross-release rollback
  protection; those trust upgrades remain explicit later work.
- The attempted operational full-graph query cache is rejected and removed.
  Independent review found pathname/descriptor TOCTOU gaps, crash-poisoned
  publication state, and unacceptable construction latency: the live 633 MiB
  graph missed the 120-second deadline and remained unfinished in a longer
  probe. Do not restore or count that cache as progress without a new design
  and fresh review.
- The corrected first-slice catalog route is a small release-pinned,
  exact-hash benefit-eligible CTX catalog compiled at setup/update time,
  followed by a query-scoped exact
  benefit closure of at most 512 candidates. Only capabilities with reviewed,
  provenance-bound benefit profiles may enter positive-benefit planning.
  User and organization graphs remain required inputs to the eventual product,
  but their candidates become executable only after their own explicitly
  trusted profile and precedence authorities exist; graph prose or install
  consent alone never grants benefit or execution authority.
- The first `ctx run` cut is new-session-only and supports `legacy`, `shadow`,
  and `recommend`, defaulting to `legacy` with an emergency override. All
  engine work and exact payload-bound checks finish before the first provider
  call. Engine failure selects the unchanged legacy path for that call and
  trips a session-level circuit breaker; a zero-capability abstention is a
  successful engine decision, not a fallback. Resume never reopens or
  reprojects the decision and cannot silently restore the legacy selector;
  per-turn replanning, installation, activation, cooling, and unload remain
  outside this slice.
  The closed query session, one-use controller, CLI precedence and emergency
  override, legacy-byte compatibility, safe metadata, recommendation
  presentation, abstention, session breaker, resume non-reprojection, and
  suppression of legacy discovery, content-loading, recommendation, and effect
  surfaces are now covered by 195 focused passing tests. The proportional
  changed-surface gate passed 1,436 tests before the final exact-type hardening,
  which then passed its focused and static checks. Independent correctness and
  security re-reviews found no remaining P0, P1, P2, or P3 finding. A later
  host-mapping review reopened one receipt-coherence claim: rendered context
  had been stored independently from its committed plan, and positive tests
  forged an impossible presented outcome under the abstention-only release.
  The repair now derives context only from exact safe selections after the
  full schema-v3 action rows and plan digest match the committed plan. A
  host-neutral semantic presentation digest, a separate host/journal receipt
  digest, and a process-local seal bound to the original receipt reject
  context, host, release, and journal substitutions; failures are a separate
  bounded value. The migrated focused slice passes 202 tests, and a
  broader engine/runtime/CLI/security/package gate passes 1,377 tests. The
  repaired query-only `ctx run` slice is accepted; positive production
  recommendations remain blocked by the intentionally abstaining shipped
  catalog.
- Cooling/deactivation receipts and leases exist; opportunity-window
  hysteresis, unload prompting, and a separately authorized persistent
  uninstall workflow are not complete.
- The paired baseline-versus-CTX campaign with identical tasks, models, tools,
  approvals, and budgets remains the decisive product gate. No current result
  proves lower tokens, time, or cost at equal-or-better quality.
- Query-hook crash-before-terminal recovery and fixed-name/count-bounded scratch
  retention now have focused POSIX evidence, including concurrent crash storms,
  bounded enumeration, oversized authenticated managed-file cleanup, and
  fail-closed unknown or unsafe residue. This boundary assumes cooperative
  processes under one OS user; unrelated-user rename attacks are excluded by
  protected ancestry, while malicious same-user namespace races are not claimed
  as an OS isolation boundary. Broader engine subprocess recovery, legacy
  import/read, replay performance, native Windows evidence, and long-lived
  audit-store retention remain pre-release residuals.

Stage 4 checkpoint:

- The first real shipped-catalog, no-provider-token dry pair was a useful
  failure: the baseline prompt stayed byte-identical, but full graph rebuilding
  took 15.93 seconds and returned five weak or duplicate skills. That treatment
  remains recorded as a relevance/latency no-go.
- A historical authenticated indexed-source prototype planned each current
  canary in about 0.4 seconds and selected one exact project-owned skill for the
  Click and Requests probes. Its positive relevance result remains useful, but
  its full-graph cache construction path has since failed production-scale
  security and latency review and is no longer an accepted runtime design.
- The versioned treatment now uses the engine lifecycle rather than prompt
  concatenation alone: committed bundle, reassessment, exact activation action
  and receipt, turn preparation action, authenticated content preparation, and
  prepared receipt. A ready dry run has six journal records. Raw capability
  content is excluded from replay/journal bytes, and a dry run never claims
  provider submission or use.
- Independent P0/P1 re-review now passes. Fresh production-catalog dry pairs for
  Click and Requests also pass with no incidents: each baseline prompt remained
  byte-identical, each treatment selected and prepared only
  `skill:ctx-python-api-compatibility`, and each treatment journal contained six
  records with neither raw skill content nor a provider-submission claim.
  Engine setup/planning/content/render totaled 1.134 seconds for Click and 1.064
  seconds for Requests in that historical prototype. These measurements do not
  validate the removed cache or the replacement eligible-catalog design.
- These are wiring, relevance, authorization, and latency results—not product
  benefit evidence. The next Stage 4 gate is a benchmark-methodology review of
  the versioned treatment and a fresh task-disjoint live campaign. Paid/live
  execution remains disabled until that review approves the design and the
  provider-submission/teardown evidence path exists.

### Stage 1: Contract freeze

Owner: engine coordinator. Parallel inputs: protocol designer, compatibility
mapper, adversarial policy reviewer. Independent architecture reviewer approves
the synthesized contract.

Deliverables:

- architecture decision and domain language;
- protocol and receipt schema;
- host feature matrix;
- compatibility characterization matrix;
- per-kind opportunity, exposure, invocation, effectiveness, validation, idle,
  and counter-evidence semantics and fixtures;
- acceptance and negative gates.

Exit: every transition and failure path is specified; unresolved semantics are
a no-go for implementation.

### Stage 2: Reducer and persistence

Parallel lanes with disjoint ownership:

- protocol/state/reducer;
- an authoritative SQLite journal with atomic event, replay input, transition,
  and projection commits; rebuildable projections; legacy import;
- property, replay, concurrency, crash, and privacy tests.

The revision stream is tenant, workspace, repository, and current-work session;
child exposures share that stream and cannot create independent five-item
belts. Duplicate lookup precedes revision comparison. The journal, not reducer
state, owns event-ID collision detection and cached transitions. V1 ships the
durable SQLite implementation directly rather than allowing an in-memory store
to hide cross-process, crash, or permission failures.

Reviewer: state-machine/failure-injection critic.

Exit: deterministic replay, zero invalid physical states, correct idempotency,
reference-counted leases, and legacy read compatibility.

### Stage 3: Host-neutral recommendation walking skeleton

Connect a typed current-work observation to a frozen catalog source, one
deterministic all-type planner, a durable replayed decision, and a committed
`PresentBundle`. Project that exact bundle through recommendation-only Codex and
Claude Code adapters with no host-specific ranking, filtering, installation, or
activation. Keep the shared host context compact even when the durable evidence
is large.

Reviewer: engine/replay/host contract reviewer.

Exit: global zero-to-five selection, abstention, no replanning on replay,
catalog/planner binding, authoritative active-set exclusion, frozen graph input,
and identical renderer output pass end to end. No valid committed plan may be
unrenderable.

### Stage 4: Early product-benefit experiment

Integrate the new engine—not a legacy recommender—into the existing isolated,
paired baseline-versus-treatment benchmark. Both lanes receive the same
repository, commit, task bytes, model, base tools, approvals, and budgets. The
treatment receives the compact committed bundle plus, under the explicit
experiment authorization, the exact authenticated content for at most the first
committed loadable skill. That content is exposed ephemerally through an
engine-issued action and remains within the total host-context budget. Hidden
evaluation owns correctness; exact parent/child tokens, elapsed time,
provider/tool cost, recommendation latency, bundle content, exposure, and
invocation are recorded.

Begin with a cheap smoke pair that verifies isolation and attribution. Then run
a small task-disjoint campaign. If recommendations are irrelevant, duplicated,
too slow, unused, or do not plausibly improve a primary outcome without harming
quality or safety, stop and revise retrieval/observation/ranking before adding
lifecycle machinery.

Reviewer: benchmark-methodology and contamination reviewer.

Exit: an honest beneficial, not-beneficial, or insufficient-evidence verdict
with reproducible artifacts. Only a beneficial or clearly actionable
insufficient-evidence result advances to lifecycle investment.

### Stage 5: Catalog, analyzers, and relevance hardening

Replace full-graph reconstruction in the current-work loop with a frozen,
indexed candidate source. Add canonical identity, equivalence,
provides/requires/conflicts, source precedence, actionability, calibrated
abstention, set-level budgets, and typed install/action plans. Implement
structured incremental analyzers for task/subtask ownership, diffs,
diagnostics, errors, tests, and validations.

Before ranker tuning, freeze development and untouched holdout corpora, labels,
metrics, thresholds, adjudication, and access rules. The first claimed matrix is
C, C++, C#, Go, Java, JavaScript, PHP, Python, Rust, and TypeScript, including
negative/abstention tasks. Ranking implementers must not see holdout labels.

Reviewer: blinded recommendation/evidence evaluator.

Exit: snapshot reproducibility, duplicate control, actionability, latency,
abstention, per-language relevance, safety, and old-surface non-regression gates
pass.

### Stage 6: Full capability session and `ctx run`

Implement the evidence ladder, multi-owner leases, cooling, deactivation, and
verified action reconciliation. Reuse or wrap only the existing ephemeral skill
and preconfigured-MCP actuators needed for the reference `ctx run` slice, and
progress it through shadow, recommend, and opt-in activate modes. Persistent
`manage` remains disabled until Stage 7 supplies typed durable actuators and
their security/rollback evidence; unsupported agents and harnesses stay
deferred.

Reviewer: lifecycle/churn critic.

Exit: longitudinal simulations and canary traces demonstrate useful stability,
bounded prompt burden, zero false applied/revoked state, and reliable rollback.

### Stage 7: Actuators and adapters

Implement typed capability actuators, enable persistent `manage` only after
their consent, security, verification, and rollback gates pass, then migrate in
fidelity order:

The setup command and persisted per-kind consent-policy choices below are
already implemented. The current Stage 7 slice also includes the accepted
journal-bound one-use claim/outcome/settlement path, non-serializable one-shot
driver boundary, kind-aware registry, one verified POSIX skill actuator, one
verified POSIX inactive-agent artifact actuator with crash reconciliation, and
one guarded installed-skill lifecycle activation path. The first later-turn
host exposure path now works through the shared Codex/Claude controller for the
reviewed release skill without adding a second planner or exceeding the global
zero-to-five set. General agent host projection/activation, MCP and native-
Windows drivers, deactivation rollback, and live-host proof remain open.

- add setup/onboarding and configuration migration for independently selecting
  `preapproved-auto` or `ask-each-time` for skills, agents, and MCP servers;
- persist the canonical consent-policy snapshot outside graph data and expose
  the resolved choice in host-neutral status output;
- make preapproved decisions automatic only at the presentation layer: they
  still traverse the exact journaled consent, install, receipt, and activation
  state-machine path;

1. Python and MCP transport;
2. Claude Code;
3. Codex;
4. LoopFlow compatibility.

Run adapter work in parallel only after the protocol and shared conformance kit
are frozen. Each adapter has one writer and one independent reviewer.

Exit: every advertised capability level passes normalized traces plus real-host
activation/revocation proof. Unsupported behavior is reported honestly.

### Stage 8: Production proof, organization layers, and retirement

Add local/user/organization catalog layers after local engine value is proven.
Run live-host canaries, longitudinal churn/consent evaluation, and a larger
fresh task-disjoint paired benefit campaign for the production engine.

The current production runtime consumes one authenticated frozen graph
artifact; it does not yet merge local, user, and organization layers.

Reviewer lanes:

- statistical/methodology reviewer;
- security/privacy reviewer;
- packaging/platform reviewer;
- final architecture reviewer.

Retire legacy paths only after at least two stable releases, a rollback-tested
soak period, no direct callers, migration replay, package evidence, native
Windows evidence, and an independently reviewed product verdict.

## Dispatch and review rules

- One coordinator owns scope, shared interfaces, integration order, and final
  synthesis.
- Parallel agents receive bounded semantic lanes or disjoint file ownership.
- No production host adapter begins before the protocol and conformance fixtures
  are frozen. Stage 3's reference walking skeleton is the fixture producer, not
  a host cutover.
- Every material actor output receives an independent critic before integration.
- Actor and reviewer disagreement remains blocked for coordinator resolution.
- No finding counts without source, command, test, or artifact evidence.
- Focused tests run before repository-wide gates.
- Changes touching more than five files should normally be split into a smaller
  vertical slice unless the coordinator documents why atomicity requires them.

## Verification ladder

1. Contract and golden serialization tests.
2. Property/state-machine, replay, crash, concurrency, lease, and privacy tests.
3. Fake-host conformance at query, observe, activate, and manage levels.
4. Offline multi-language recommendation evaluation.
5. Clean-install, upgrade, migration, rollback, packaging, and platform tests.
6. Real-host conformance for `ctx run`, Claude Code, and a Codex/MCP path.
7. Shadow and opt-in canary rollout with kill switch.
8. Longitudinal task-shift and consent-burden study.
9. Paired hidden-evaluator product-benefit campaign.
10. Legacy retirement evidence.

Each material PR runs focused tests, then `scripts/no_mistakes_run.sh fast`, then
`python scripts/ci_preflight.py --profile pr`. Local evidence does not replace
required native Windows or live-host evidence.

## Product evidence rule

One same-repository baseline-versus-CTX pair is a demonstration, not proof. The
production claim requires a preregistered, task-disjoint, paired experiment with
identical repository, commit, task bytes, model, base tools, approvals, and
budgets; hidden evaluation; verified recommendation, exposure, invocation, and
effect evidence; complete parent and child cost; and an honest beneficial,
not-beneficial, or insufficient-evidence result.

Primary outcomes are task correctness/quality, time to accepted result, total
input and output tokens, provider/tool cost, and wall-clock time. Secondary
outcomes are bundle precision, useful-capability recall, abstention quality,
acceptance, verified invocation/effect, recommendation latency, churn, approval
burden, and security failures. Report paired distributions and uncertainty, not
only averages. CTX earns the product claim only if it improves at least one
primary efficiency outcome without materially degrading correctness or safety;
otherwise the verdict is not beneficial or insufficient evidence.

The current skill-only benchmark remains a separate narrow treatment. It must
not be relabeled as evidence for the unified adaptive engine.
