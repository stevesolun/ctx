# Entity Onboarding

ctx treats skills, agents, MCP servers, and harnesses as wiki entities that can
be indexed, linked in the knowledge graph, and recommended from the same
surface. The important distinction is install behavior:

- Skills and agents are local Claude Code assets.
- MCP servers are cataloged first, then installed only when the user opts in.
- Harnesses are cataloged first. A harness describes the machinery around the
  model: runtime, tools, access boundaries, memory, verification, and approval
  policy. Adding one never executes upstream setup commands.

After adding any entity, rebuild the graph when you want it to participate in
recommendations:

```bash
ctx-wiki-graphify
ctx-scan-repo --repo . --recommend
```

## Add a Skill

Use this when you have a local `SKILL.md` that should be installed under
`~/.claude/skills/<name>/SKILL.md` and mirrored into the wiki.

```bash
ctx-skill-add \
  --skill-path ./SKILL.md \
  --name fastapi-review
```

What happens:

1. The name is validated.
2. Intake checks run against the markdown.
3. The skill is copied into `~/.claude/skills/`.
4. A wiki page is created under `entities/skills/`.
5. The wiki index and log are updated.

## Add an Agent

Use this when you have a local Claude Code agent markdown file.

```bash
python -m agent_add \
  --agent-path ./code-reviewer.md \
  --name code-reviewer
```

Batch-add every top-level `.md` file in a directory:

```bash
python -m agent_add --scan-dir ./agents --skip-existing
```

Agents are copied into `~/.claude/agents/` and mirrored into
`entities/agents/`. Re-run `ctx-wiki-graphify` after adding agents if you want
graph recommendations to include them.

## Add an MCP Server

Use this when you want the MCP server available as a recommendation before
installing it into a host.

Create `github-mcp.json`:

```json
{
  "name": "GitHub MCP",
  "slug": "github-mcp",
  "description": "MCP server for GitHub repository and issue workflows.",
  "github_url": "https://github.com/modelcontextprotocol/servers",
  "sources": ["manual"],
  "tags": ["github", "automation", "repository"],
  "transports": ["stdio"]
}
```

Add it:

```bash
ctx-mcp-add --from-json ./github-mcp.json
```

MCP pages live under `entities/mcp-servers/<shard>/<slug>.md`. The add command
deduplicates by canonical GitHub URL when possible, so two catalogs pointing at
the same upstream repository merge into one entity.

## Add a Harness

Use this when a repo provides the runtime around a model rather than just a
tool. Harness examples include coding-agent loops, CAD-generation runtimes,
browser-automation runners, evaluation loops, and local-model workbenches.

Example: catalog `earthtojake/text-to-cad` as a harness recommendation.

```bash
ctx-harness-add \
  --repo https://github.com/earthtojake/text-to-cad \
  --name "Text to CAD" \
  --description "Harness for turning text prompts into CAD artifacts." \
  --tag cad --tag 3d --tag automation \
  --model-provider openai \
  --runtime python \
  --capability "Generate CAD artifacts from natural language" \
  --setup-command "pip install -e ." \
  --verify-command "pytest"
```

Or load one JSON record:

```json
{
  "repo_url": "https://github.com/earthtojake/text-to-cad",
  "name": "Text to CAD",
  "description": "Harness for turning text prompts into CAD artifacts.",
  "tags": ["cad", "3d", "automation"],
  "model_providers": ["openai"],
  "runtimes": ["python"],
  "capabilities": ["Generate CAD artifacts from natural language"],
  "setup_commands": ["pip install -e ."],
  "verify_commands": ["pytest"],
  "sources": ["manual"]
}
```

```bash
ctx-harness-add --from-json ./text-to-cad-harness.json
```

Harness pages live under `entities/harnesses/<slug>.md`. Setup and verification
commands are documentation only; ctx records them so the user can inspect and
decide before running anything.

## Initialize Model Choice

During setup, record whether you use Claude Code or your own model. Plain
`ctx-init` starts a small wizard when it is attached to an interactive
terminal; use `ctx-init --wizard` to force the prompts, or pass explicit flags
such as `--model-mode skip` for non-interactive automation.

```bash
ctx-init
ctx-init --wizard
ctx-init --model-mode skip
```

For Claude Code:

```bash
ctx-init --model-mode claude-code --goal "maintain a FastAPI service"
```

For a custom model:

```bash
ctx-init \
  --model-mode custom \
  --model openai/gpt-5.5 \
  --goal "build CAD artifacts from text prompts"
```

Add `--validate-model` only when you want `ctx-init` to make one small provider
call. Without that flag, setup writes `~/.claude/ctx-model-profile.json` and
prints harness recommendations without calling the model.
