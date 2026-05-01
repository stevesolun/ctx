# Attaching ctx to any LLM host

`ctx` ships three integration surfaces. Pick based on what your host
already supports:

| Your host | Use |
|---|---|
| MCP-native (Claude Code, Claude Agent SDK, Cline, Goose, OpenHands, Continue) | **MCP server** — no Python, just spawn `ctx-mcp-server` |
| Anything that isn't MCP-native but runs Python | **Python library** — `from ctx import recommend_bundle, ...` |
| "I just want to run an agent and get recommendations" | **`ctx run` CLI** — our built-in harness |

All three paths consume the **same** knowledge graph, llm-wiki, and
quality scoring. Recommendations are identical; only the transport
differs.

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

---

## Choosing the right path

| Situation | Path |
|---|---|
| Your host already speaks MCP | 1 (MCP server) — zero Python code on your side |
| You want the alive-skill system inside your existing Python loop | 2 (library) |
| You're comparing models and need a harness | 3 (CLI) |
| You're building an IDE extension | 1 if the IDE speaks MCP (most do), else 2 |

All three paths share `~/.claude/skill-wiki/` as the source-of-truth
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
