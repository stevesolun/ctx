# Attaching ctx to any LLM host

`ctx` ships four integration surfaces. Pick based on what your host
already supports:

| Your host | Use |
|---|---|
| MCP-native (Claude Code, Claude Agent SDK, Cline, Goose, OpenHands, Continue) | **MCP server** — no Python, just spawn `ctx-mcp-server` |
| Anything that isn't MCP-native but runs Python | **Python library** — `from ctx import recommend_bundle, ...` |
| "I just want to run an agent and get recommendations" | **`ctx run` CLI** — our built-in harness |
| LoopFlow or another loop that already owns plan/act/observe | **LoopFlow adapter** — `python -m ctx.adapters.loopflow` before planning |

All four paths consume the **same** knowledge graph, llm-wiki, and
quality scoring inputs. Output shape and grouping can differ by host
surface; the transport and permission contract decide what each loop sees.

---

## 1. MCP server path

Install ctx with the harness extras:

```bash
pip install "claude-ctx[harness]"
```

This puts `ctx-mcp-server` on your PATH. Then wire it into your host:

### Claude Code

```bash
claude mcp add ctx-wiki -- ctx-mcp-server
```

The tools `ctx__recommend_bundle`, `ctx__graph_query`, `ctx__wiki_search`,
`ctx__wiki_get` appear to Claude on the next turn. Ask
"What skills help with FastAPI auth?" and it will call them.

### Claude Agent SDK (Python)

```python
from anthropic import Anthropic
from claude_agent_sdk import ClaudeAgentOptions, McpServerConfig

options = ClaudeAgentOptions(
    mcp_servers={
        "ctx-wiki": McpServerConfig(
            command="ctx-mcp-server",
        ),
    },
)
```

### Cline / Continue.dev

Add to your MCP server config (`~/.config/cline/mcp.json` or the
Continue equivalent):

```json
{
  "mcpServers": {
    "ctx-wiki": {
      "command": "ctx-mcp-server"
    }
  }
}
```

### Goose

`~/.config/goose/config.yaml`:

```yaml
extensions:
  ctx-wiki:
    type: stdio
    cmd: ctx-mcp-server
```

### OpenHands

OpenHands' runtime config:

```json
{
  "mcp_servers": {
    "ctx-wiki": {
      "command": "ctx-mcp-server"
    }
  }
}
```

### Any MCP-speaking harness

The server reads JSON-RPC 2.0 on stdin, writes on stdout, speaks
MCP protocol version `2024-11-05`. Any client that does the standard
`initialize` handshake + `tools/list` + `tools/call` flow works.

### Live MCP compatibility gate

The regular test suite never starts arbitrary third-party MCP servers.
Those commands run as local subprocesses and can read files, use the
network, and inherit whatever environment you explicitly allow.

To validate a trusted server, provide a local config and opt in:

```bash
python -m pytest src/tests/test_mcp_live_compat.py \
  --run-live-mcp \
  --live-mcp-config /path/to/trusted-mcp.json
```

Example config:

```json
{
  "name": "trusted-filesystem",
  "command": "npx",
  "args": ["-y", "@modelcontextprotocol/server-filesystem", "${tmp_path}"],
  "startup_timeout": 30,
  "request_timeout": 10,
  "inherit_env": false,
  "env": {},
  "expected_tools": ["list_directory"],
  "probe": {
    "tool": "list_directory",
    "arguments": {"path": "."},
    "expect_text_contains": ""
  },
  "trust": {
    "server_is_third_party_code": true,
    "approved_by": "your-name"
  }
}
```

`command` and `args` are passed as an argv list, not through a shell.
Parent secrets are not inherited unless you set `inherit_env: true`; prefer
explicit `env` keys for servers that need credentials. `${tmp_path}` expands
to a pytest temporary directory so filesystem probes can avoid real user data.

---

## 2. Python library path

For custom harnesses that aren't MCP-native but can import Python:

```python
from ctx import (
    recommend_bundle,   # free-text → ranked skill/agent/MCP bundle
    graph_query,        # walk from seed entities
    wiki_search,        # keyword search entity pages
    wiki_get,           # fetch one entity by slug
    list_all_entities,  # enumerate every slug
)

# Inside your agent loop:
def on_user_turn(query: str):
    bundle = recommend_bundle(query, top_k=5)
    for entry in bundle:
        print(f"  [{entry['type']:>11}] {entry['name']}  (score {entry['score']:.1f})")

    # User asks about a specific slug you saw in the bundle:
    page = wiki_get("fastapi-pro")
    if page:
        inject_into_context(page["body"])
```

The first call to any of these lazy-loads the graph + wiki once;
subsequent calls are O(walk) cheap. Safe to call from inside your
own while-loop on every turn.

Advanced: build a `CtxCoreToolbox` directly if you need to point at
a non-default wiki/graph path:

```python
from pathlib import Path
from ctx import CtxCoreToolbox

toolbox = CtxCoreToolbox(
    wiki_dir=Path("/path/to/custom/wiki"),
    graph_path=Path("/path/to/custom/graph.json"),
)
for td in toolbox.tool_definitions():
    print(td.name, td.description[:50])
```

---

## 3. `ctx run` CLI path

If you don't have your own loop yet:

```bash
pip install "claude-ctx[harness]"
export OPENROUTER_API_KEY=sk-or-v1-...

ctx run \
    --model openrouter/anthropic/claude-opus-4.7 \
    --task "find the failing tests in this repo and fix them" \
    --mcp filesystem \
    --budget-usd 2.00
```

Or offline with Ollama:

```bash
ctx run \
    --model ollama/llama3.1:70b \
    --task "summarize the architecture" \
    --mcp filesystem
```

See `ctx run --help` for the full flag set (budgets, compaction,
system prompt overrides, session resume, JSON output, ...).
If the console script is unavailable, use the package entrypoint instead:
`python -m ctx run ...`.

Planning and review modes are opt-in flags on `ctx run`. Use `--planner` to
produce a structured spec before generation, `--evaluator` to grade and revise
the result, and `--contract` with both `--planner` and `--evaluator` to refine
testable success criteria before the generator starts:

```bash
ctx run \
    --model openrouter/anthropic/claude-opus-4.7 \
    --task "implement the checkout retry policy" \
    --planner \
    --evaluator \
    --contract
```

Resume keeps executable MCP metadata disabled by default. To replay the saved
messages and recreate ctx-core tools for a new task, run:

```bash
ctx resume <session-id> --task "..."
```

It skips recorded MCP command metadata unless you pass
`--restore-session-mcp`. When MCP restoration is enabled, session metadata
stores only `credential_env` names and reads those variables from the current
process environment; secret values are not stored in the session log.

---

## 4. LoopFlow and agent-loop adapter path

DSL runners such as [LoopFlow](https://loopflow.live/) and custom agent loops
already own the control flow: plan, act, observe, reflect, and stop when their
gate passes. Use `python -m ctx.adapters.loopflow` when that loop should ask
ctx which capabilities it may load before planning.

For a presenter-ready walkthrough, see
[LoopFlow adapter demo](loopflow-adapter-demo.md).

The adapter emits a JSON contract with:

- explicit permission grants for `skills`, `agents`, `mcps`, and `harnesses`;
- the `ctx-mcp-server` command and ctx tool names when the permission contract
  allows ctx-core tools;
- ranked skill, agent, and MCP recommendations from the `ctx-recommend`
  engine;
- optional harness recommendations only when the loop declares a user-owned,
  API, or local model.

For LoopFlow, keep the `.loop` file in charge and call ctx before the plan:

```bash
python -m ctx.adapters.loopflow \
  --loop-file rate-limit.loop \
  --permissions skills,agents,mcps
```

Add `--last-failure-file .loopflow/last-failure.txt` only after the loop has
written that file; omit it on the first run. The adapter uses that failure text
for recommendation ranking and returns only `context.last_failure_present`, not
the raw failure.

The returned payload includes LoopFlow-ready hints for the granted groups:

```loop
use skills: security-review, code-review
```

Only installed/local skill rows are named in `loopflow.use_skills`. Installable
catalog skills remain under `capabilities.skills` with `status: available` and
their `install_command` metadata.

When the LoopFlow run uses its own LLM rather than a hosted Claude Code
session, grant harnesses and pass the model profile:

```bash
python -m ctx.adapters.loopflow \
  --loop-file private-agent.loop \
  --permissions skills,agents,mcps,harnesses \
  --own-llm \
  --model-provider ollama \
  --model ollama/llama3.1 \
  --harness-runtime "local workstation" \
  --harness-tools "filesystem, shell, browser" \
  --harness-privacy "no cloud prompts"
```

Other harness matching hints use the same names as the install flow:
`--harness-autonomy`, `--harness-verify`, `--harness-attach-mode`, and
`--api-key-env`.

Generic agent loops can import the same adapter directly:

```python
from ctx.adapters.loopflow import recommend_for_loop

plan_context = recommend_for_loop(
    goal="fix checkout e2e flake",
    loop_kind="agent-loop",
    look_at=["tests/e2e", "playwright config"],
    done_when=['"pytest tests/e2e -q" passes'],
    last_failure=last_failure_text,
    permissions={"skills", "agents", "mcps", "harnesses"},
    own_llm=True,
    model_provider="openrouter",
    model="anthropic/claude-opus-4.7",
)
```

Load only the groups that are explicitly granted in `permissions`. If
`harnesses` is granted without `--own-llm`, `--model-provider`, or `--model`,
the adapter returns a warning and no harness recommendations. The ctx MCP
command and tool list are also permission-filtered; ctx tools that can operate
across all capability groups only appear when all of those groups are granted.

---

## Installed harness attachment

`ctx-harness-install <slug>` creates `.ctx/attach/` inside the installed
harness target. The directory contains the attach files for the modes that
catalog entry supports:

- `README.md` describes the supported modes and safety expectations.
- `mcp.json` starts `ctx-mcp-server` for MCP-speaking hosts.
- `python.py` shows the Python recommendation/wiki calls for custom loops.
- `ctx-run.txt` gives a `ctx run` command template.

The install command does not run the harness or store secrets in those files.
Setup commands still require `--approve-commands`; verification commands still
require `--run-verify`.

If no catalog harness fits, generate a build handoff instead of forcing a weak
match:

```bash
ctx-harness-install --recommend \
  --goal "build a private CAD workflow with a local model" \
  --model-provider ollama \
  --model ollama/llama3.1 \
  --plan-on-no-fit \
  --plan-output custom-harness.md
```

---

## Choosing the right path

| Situation | Path |
|---|---|
| Your host already speaks MCP | 1 (MCP server) — zero Python code on your side |
| You want the alive-skill system inside your existing Python loop | 2 (library) |
| You're comparing models and need a harness | 3 (CLI) |
| No catalog harness fits your model/goal | generated custom harness plan |
| You're building an IDE extension | 1 if the IDE speaks MCP (most do), else 2 |
| You're building a DSL runner or agent loop | 4 (LoopFlow adapter) |

All four paths share `~/.claude/skill-wiki/` as the source-of-truth
corpus, so your recommendations are consistent regardless of the
integration you pick.

---

## Skill lifecycle

Recommendations go up and down based on use automatically. `ctx`
tracks:

- **How recently a skill was invoked** (`telemetry_signal`).
- **How broadly it's used across the graph** (`graph_signal`).
- **Whether new skills are being added** (`intake_signal`).

Skills that fall below a quality floor get demoted to `stale` status
and de-ranked from future recommendations. This logic lives in
`ctx.core.quality.quality_signals` and runs identically whether
you're on the MCP path, library path, or `ctx run` CLI.

To inspect lifecycle state for a specific skill:

```bash
ctx-skill-quality explain fastapi-pro
```

Or from Python:

```python
from ctx.core.quality import quality_signals
# see ctx.core.quality for the scoring API
```
