# Skill router

!!! info "Recommendations from your own knowledge"

    CTX ships the engine and empty wiki/graph structure, not a tool catalog.
    This page describes repository routing over user-supplied records.
    The optional `ctx fit` evaluator is separate. See the [home page](../index.md).


The skill router suggests skills, agents, and MCP servers relevant to the
current development task, using records you or your organization supply.
CLI, MCP, Python, and connected native hosts share the recommendation policy.
Harness onboarding remains a separate advanced workflow.

## Problem

Every loaded skill, agent, and MCP server costs tokens, attention, and tool
surface area. A session can benefit from a small relevant subset of your own
knowledge. An empty wiki or a task with no useful match can return no suggestions.
Loading too much:

- wastes context on irrelevant instructions,
- causes the wrong helper to trigger for a task,
- slows response time, and
- creates conflicting instructions between helpers.

## Architecture

```text
ctx/
|-- src/ctx/adapters/generic/ctx_core_tools.py # Shared graph/wiki recommendations
|-- src/ctx/runtime/user_recommendations.py   # Advisory native-host projection
|-- src/ctx/core/wiki/                       # User wiki authoring and indexing
`-- src/scan_repo.py                         # Optional repository stack scan
```

## Flow

1. Initialize an empty local wiki with `ctx-init --model-mode skip`, then
   [author or import your own records](../entity-onboarding.md).
2. Ask `ctx recommend "review a Python API"`, call `ctx__recommend_bundle`
   through MCP, or use a connected native host's prompt events.
3. The shared engine applies the
   [context and availability policy](../harness/attaching-to-hosts.md#2-python-library-path)
   to return a bounded mixed shortlist. An empty result is valid.
4. The host presents the bounded suggestions. Native hooks expose only bounded
   identifiers, matching tags, and availability. Graph content is untrusted
   data and grants no installation, execution, activation, or unload authority.
5. Install a selected tool only after explicit approval through a separate
   user/host workflow. Saved legacy preapproval settings do not authorize the
   recommendation path to act. Unload and uninstall require separate permission.

Default setup is local and offline, with no bundled corpus or implicit catalog
download. Recommendations do not call a paid model or claim measured savings.

## Advanced compatibility workflows

`ctx-scan-repo --repo . --recommend` derives repository stack signals and offers
a scanner-specific view of the local recommendations. The separate
`ctx.core.resolve.resolve_skills` module can produce a proposed load/unload
manifest for older integrations. Neither output executes those actions. These
compatibility workflows are separate from the normal task-query flow above.

## Reference Pages

- [Stack signatures](../stack-signatures.md) - implemented scanner boundary
  and proposed detection coverage.
- [Skill-stack matrix](../skill-stack-matrix.md) - implemented resolver boundary
  and proposed stack-to-capability mappings.
- [Entity source surfaces](../marketplace-registry.md) - discovery inputs,
  ingestion validation, and explicit update rules.
