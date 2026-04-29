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

## Updating the Graph and LLM Wiki

Use this sequence for every accepted skill, agent, MCP server, or harness
change. The graph and LLM-wiki are shippable artifacts, not scratch output, so
the update is treated like a release step.

1. Add or update the entity through the matching command:
   `ctx-skill-add`, `ctx-agent-add`, `ctx-mcp-add`, or `ctx-harness-add`.
2. If the entity already exists, read the update review. It lists changed
   fields, likely benefits, regressions, and security findings. Do not pass
   `--update-existing` until those findings are acceptable.
3. Run the security/cyber check below.
4. Rebuild the curated wiki graph with `ctx-wiki-graphify`.
5. Repack `graph/wiki-graph.tar.gz` with the exclusions in
   `graph/README.md`; never commit local review reports or raw caches.
6. Refresh the Skills.sh external overlay when shipping catalog coverage.
   This adds catalog-only `external-skill` graph nodes, sharded metadata
   pages, install commands, duplicate hints, and metadata-only
   quality/security signals:

   ```bash
   python src/import_skills_sh_catalog.py --from-api-union <raw.json> \
     --catalog-out graph/skills-sh-catalog.json.gz \
     --wiki-tar graph/wiki-graph.tar.gz \
     --update-wiki-tar
   ```
7. Refresh published counts with `python src/update_repo_stats.py`.
8. Verify the changed entity can be recommended through
   `ctx-scan-repo --repo . --recommend` or `ctx__recommend_bundle`.

## Security and Cyber Check

Run this before applying `--update-existing`, before installing a harness with
approved commands, and before shipping a refreshed graph tarball.

- Inspect changed entity markdown and frontmatter for shell commands, setup
  commands, install commands, URLs, requested permissions, and model/provider
  access.
- Treat these as manual-review blockers: `curl | sh`, `wget | bash`,
  `Invoke-Expression`, broad `rm -rf`, `git reset --hard`, `chmod 777`, secret
  upload, disabled auth/TLS/sandboxing/audit/tests, or unpinned package sources.
- For MCP and harness updates, check network access, filesystem scope, auth
  material, command transports, and whether setup or verify commands execute
  remote code.
- Prefer dry-run first: `ctx-harness-install <slug> --dry-run` and
  `ctx-harness-install <slug> --update --dry-run`.
- If a candidate is useful but risky, document the safer install path or keep it
  as catalog-only metadata instead of shipping it as an installed skill.

## Updating an Existing Entity

The add commands are non-destructive by default when the target skill, agent,
MCP server, or harness already exists. The first add attempt prints an update
review instead of replacing files. That review lists changed fields, expected
benefits, possible regressions, security findings, and a recommendation.

Use this flow for every entity type:

1. Run the normal add command.
2. If ctx prints `Existing <type> already exists`, read the benefits and risks.
3. Keep the current entity by doing nothing, or re-run with `--skip-existing`
   in batch jobs where you do not want reviews.
4. Apply the replacement only after review with `--update-existing`.
5. Rebuild the graph with `ctx-wiki-graphify` when the update should affect
   recommendations.

Examples:

```bash
ctx-skill-add --skill-path ./SKILL.md --name fastapi-review
ctx-skill-add --skill-path ./SKILL.md --name fastapi-review --update-existing

ctx-agent-add --agent-path ./code-reviewer.md --name code-reviewer
ctx-agent-add --agent-path ./code-reviewer.md --name code-reviewer --update-existing

ctx-mcp-add --from-json ./github-mcp.json
ctx-mcp-add --from-json ./github-mcp.json --update-existing

ctx-harness-add --from-json ./text-to-cad-harness.json
ctx-harness-add --from-json ./text-to-cad-harness.json --update-existing
```

`ctx-harness-install --update` is different: it refreshes an installed harness
checkout under `~/.claude/harnesses/<slug>`. Catalog entity replacement uses
`ctx-harness-add --update-existing`.

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
ctx-agent-add \
  --agent-path ./code-reviewer.md \
  --name code-reviewer
```

Batch-add every top-level `.md` file in a directory:

```bash
ctx-agent-add --scan-dir ./agents --skip-existing
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
detects existing pages by slug and, when possible, canonical GitHub URL. If a
match exists, ctx prints the update review and skips replacement unless
`--update-existing` is passed.

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

To inspect and install a cataloged harness:

```bash
ctx-harness-install text-to-cad --dry-run
ctx-harness-install text-to-cad
ctx-harness-install text-to-cad --update --dry-run
ctx-harness-install text-to-cad --uninstall --dry-run
```

The installer clones or copies the harness into `~/.claude/harnesses/<slug>` and
writes `~/.claude/harness-installs/<slug>.json`. It does not run setup commands
unless you pass `--approve-commands`, and it does not run verification commands
unless you also pass `--run-verify`.

```bash
ctx-harness-install text-to-cad --approve-commands --run-verify
ctx-harness-install text-to-cad --update --approve-commands --run-verify
ctx-harness-install text-to-cad --uninstall
ctx-harness-install text-to-cad --uninstall --keep-files
```

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
