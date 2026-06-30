# LoopFlow adapter demo

Use this page as presenter notes for the LoopFlow owner. The short version:
LoopFlow stays the control language; ctx becomes a permissioned recommendation
sidecar that tells the loop which skills, agents, MCP tools, and user-owned LLM
harnesses are worth loading before the next plan.

## 30-second pitch

LoopFlow already owns the loop: `goal:`, `look at:`, plan, act, observe,
reflect, and `done when`. ctx should not replace that. ctx should run before
planning, read the same goal, verification checks, and failure context, and
return a read-only JSON contract:

- what the loop is allowed to load;
- which skills, agents, MCP servers, and harnesses fit the current goal;
- the `ctx-mcp-server` command and tool list for MCP-aware runners;
- a dry-run harness install command only when the user declares their own
  local/API model.

## Demo loop

```loop
loop "select ctx capabilities":
  goal: mcp agent loop local ollama filesystem
  look at: the repo plan, AGENTS.md, and the last failure
  done when "pytest src/tests/test_loopflow_adapter.py -q" passes
  each cycle: plan, then act, then observe
  when it fails: reflect, then plan again
```

## Demo command

Run ctx immediately before LoopFlow plans:

```bash
python -m ctx.adapters.loopflow \
  --goal "mcp agent loop local ollama filesystem" \
  --loop-name "ctx capability selection" \
  --permissions skills,agents,mcps,harnesses \
  --own-llm \
  --model-provider ollama \
  --model ollama/llama3.1 \
  --top-k 2
```

The same call can read a real `.loop` file:

```bash
python -m ctx.adapters.loopflow \
  --loop-file select-capabilities.loop \
  --permissions skills,agents,mcps,harnesses \
  --own-llm \
  --model-provider ollama \
  --model ollama/llama3.1
```

Add `--last-failure-file .loopflow/last-failure.txt` after LoopFlow has written
that file.

## Example payload

This excerpt is from the live adapter against the current ctx catalog. Exact
recommendation names can change as the graph changes, but the contract shape is
stable.

```json
{
  "version": "ctx.loop_adapter.v1",
  "adapter": "loopflow",
  "permissions": {
    "skills": true,
    "agents": true,
    "mcps": true,
    "harnesses": true
  },
  "loopflow": {
    "before_plan": "Call python -m ctx.adapters.loopflow before planning and inject this JSON as read-only context.",
    "use_tools": "use tools from the \"ctx\" server",
    "use_skills": "use skills: oocx-tfplan2md-agent-model-selection",
    "harness_rule": "Only load harnesses when the loop runs on a user-owned/API/local LLM."
  },
  "mcp_server": {
    "name": "ctx",
    "command": "ctx-mcp-server",
    "tools": [
      "ctx__recommend_bundle",
      "ctx__graph_query",
      "ctx__wiki_search",
      "ctx__wiki_get",
      "ctx__observe_dev_event",
      "ctx__load_entity",
      "ctx__mark_entity_used",
      "ctx__record_validation",
      "ctx__record_escalation",
      "ctx__unload_entity",
      "ctx__session_end",
      "ctx__session_state"
    ]
  },
  "capabilities": {
    "skills": [
      {"name": "oocx-tfplan2md-agent-model-selection", "type": "skill", "status": "installed"},
      {
        "name": "nickcrew-claude-ctx-plugin-tool-selection",
        "type": "skill",
        "status": "available",
        "source_catalog": "skill-index",
        "install_command": "ctx-skill-install nickcrew-claude-ctx-plugin-tool-selection"
      }
    ],
    "agents": [
      {"name": "oss-investigator-local-git-agent", "type": "agent"},
      {"name": "loop-operator", "type": "agent"}
    ],
    "mcps": [
      {"name": "local-ollama-file-operations", "type": "mcp-server"},
      {"name": "multi-model-advisor-ollama", "type": "mcp-server"}
    ],
    "harnesses": [
      {"name": "autogen", "type": "harness", "fit_score": 1.0},
      {"name": "langfuse", "type": "harness", "fit_score": 1.0}
    ]
  },
  "agent_loop": {
    "before_act": "Load only the granted capability groups from capabilities.*.",
    "on_failure": "Pass the latest failure back as last_failure before the next plan.",
    "harness_install": "ctx-harness-install --dry-run '--goal=mcp agent loop local ollama filesystem' --model-provider=ollama --model=ollama/llama3.1 -- autogen"
  },
  "warnings": []
}
```

## How LoopFlow would consume it

The smallest integration is a pre-plan hook:

```python
from ctx.adapters.loopflow import recommend_for_loop

ctx_payload = recommend_for_loop(
    goal=loop.goal,
    loop_name=loop.name,
    loop_kind="loopflow",
    look_at=loop.look_at,
    done_when=loop.done_when,
    last_failure=loop.last_failure_text,
    permissions={"skills", "agents", "mcps", "harnesses"},
    own_llm=runner.uses_user_owned_model,
    model_provider=runner.model_provider,
    model=runner.model,
)

loop.add_readonly_context("ctx", ctx_payload)

if ctx_payload["mcp_server"]["command"]:
    runner.register_mcp_server(
        name=ctx_payload["mcp_server"]["name"],
        command=ctx_payload["mcp_server"]["command"],
    )

if ctx_payload["loopflow"]["use_skills"]:
    loop.add_planning_hint(ctx_payload["loopflow"]["use_skills"])
```

After a failed observe step, LoopFlow passes the new failure text back into the
next adapter call. That is the agent-loop back edge: LoopFlow keeps the retry
logic, and ctx refreshes recommendations based on what just failed.

## Permission model

The adapter fails closed:

- `--permissions skills` only returns skill recommendations.
- `--permissions mcps` only returns MCP server recommendations unless all
  capability groups are granted, which lets ctx MCP tools operate safely across
  skills, agents, MCPs, and harnesses.
- `--permissions harnesses` returns no harnesses unless `--own-llm`,
  `--model-provider`, or `--model` is present.
- `agent_loop.harness_install` is always a `--dry-run` command. It shows what
  would be installed; it does not mutate the host.

This lets a LoopFlow user decide whether a loop may use skills, agents, MCPs,
or harnesses without giving ctx authority to bypass the loop's own gates.

## Owner ask

The integration proposal for LoopFlow is intentionally small:

1. Add an optional `ctx` pre-plan hook in the LoopFlow runner.
2. Let `.loop` users grant capability groups: `skills`, `agents`, `mcps`,
   and `harnesses`.
3. Inject the returned JSON as read-only context before planning.
4. Register the ctx MCP server only when the adapter returns an MCP command.
5. Surface `agent_loop.harness_install` as an explicit user action, not an
   automatic install.

That gives LoopFlow the ctx graph, llm-wiki, MCP server, skill recommender, and
user-owned model harness recommendations while preserving LoopFlow's language,
human gates, and `done when` verification model.
