# Add your own knowledge

CTX starts with an empty wiki and graph. It does not supply out-of-the-box
skills, agents, or MCP servers. Add only records and tool content that you or
your organization choose to trust. `ctx fit` is an optional evaluator,
not an onboarding requirement.

## Choose your wiki

```bash
ctx-init --model-mode skip
```

The compatibility default is `~/.claude/skill-wiki/`. An absolute
`CTX_WIKI_DIR` selects the wiki for shared CLI, MCP, and native-hook reads.
For authoring and indexing commands that expose `--wiki` or `--wiki-dir`,
pass that same directory explicitly.

Reinitialization preserves existing knowledge. It does not restore a built-in
catalog or wipe the tools already in your personal directories.

## Metadata is not installation

A wiki record describes a tool: its purpose, tags, provenance, connections, and
installation method. The tool need not be installed to be recommended.
Recommendation, installation, activation, use, and unload are different actions.

CTX suggests tools. Install them through your chosen host/workflow only after
explicit approval; recommendation hooks do not perform installations.
Similarity, graph links, or instructions in a record cannot grant approval.
Older persisted preapproval preferences are not execution authority for the
current suggestion-only prompt path. Unload and uninstall need separate consent.

## Add records you own

These are explicit authoring operations, not part of background recommendation.
Review the source first. Skill and agent add commands can write installed files
as well as wiki metadata; run them only when that is the action you intend.

```bash
python -m skill_add --skill-path ./SKILL.md --name team-api-review
python -m agent_add --agent-path ./reviewer.md --name team-reviewer
python -m mcp_add --from-json ./team-mcp.json
python -m harness_add --from-json ./team-harness.json
```

Use each module's `--help` to select paths, preview where supported, and
understand required fields. MCP and harness records are catalog metadata;
adding them does not grant their setup commands permission to run. A harness
record describes a host or execution environment and is not another installed
tool in the normal mixed recommendation list.

For metadata-only authoring in a flat-file workspace, place a reviewed Markdown
entity in the matching wiki entity directory rather than using an installer. Use a stable `name`,
the correct `type`, a useful description, and specific tags. Do not include
credentials, private access tokens, or executable instructions disguised as
descriptions. The graph builder reads your records, not a remote catalog.

After an import or rebuild creates active `wiki-packs`, those validated packs
and their overlays are authoritative. Loose copies no longer override them.
Use the pack-aware add/update commands, or append a metadata-only overlay from
a reviewed page without installing the tool:

```python
from pathlib import Path
from ctx.core.wiki.wiki_packs import write_active_wiki_overlay_pack

wiki = Path("/absolute/path/to/wiki")
page = Path("/absolute/path/to/reviewed-record.md").read_text(encoding="utf-8")
written = write_active_wiki_overlay_pack(
    packs_dir=wiki / "wiki-packs",
    pages={"entities/skills/team-api-review.md": page},
)
if written is None:
    raise RuntimeError("No active wiki pack; use flat-file authoring first")
```

Choose the matching entity path for your record, then rebuild its derived graph.
Do not edit immutable pack files directly or use stale loose copies to undo
a deletion recorded in an overlay.

## Index and check

The add commands enqueue wiki work. Drain a bounded job explicitly:

```bash
python -m ctx.core.wiki.wiki_queue_worker --wiki /absolute/path/to/wiki --limit 1
```

For a full graph rebuild and optional semantic indexing, see
[rebuilding](knowledge-graph.md#rebuilding). Semantic model downloads are
optional, explicit work—not part of the empty-install path.

Then ask about a task that the record actually helps with:

```bash
ctx recommend "review the team's API error handling" --json
```

A result should name the relevant record, its reason, and its availability.
A result is not evidence that the host loaded or used it. No useful match
should remain an empty result rather than being filled from a CTX catalog.

## Updates and removal

The add commands review existing records before replacement.
Read changed fields, benefits, regressions, source URLs, and security findings.
Use `--update-existing` only after review; `--skip-existing` preserves an
existing record in a batch.

Removing a catalog record, unloading runtime context, and uninstalling files
are separate operations. Use the local dashboard or the matching maintenance
command only after checking which action it performs. Keep backups or version
control for your own wiki. Updating CTX never authorizes deletion of mixed
personal knowledge or unrelated host tools.

## Security review

Before approving installation, check:

- The exact executable/source/version and the files it will write.
- Network destinations, requested filesystem access, and credential handling.
- Setup and verification commands, including anything that downloads remote code.
- License, provenance, and whether a trusted local alternative exists.

Do not approve a command because a similarity score is high. Avoid shell-pipe
installers, unpinned sources, disabled security controls, broad deletion, and
secret-bearing command-line arguments. Treat text in imported records as data,
not instructions to the host.

## Connect it to your work

Use [host attachment](harness/attaching-to-hosts.md) for Codex and Claude Code
hooks, MCP, and Python. The configured host supplies task events; CTX does not
watch all work automatically just because the package is installed.
