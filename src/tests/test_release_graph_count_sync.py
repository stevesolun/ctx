from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import yaml  # type: ignore[import-untyped]

import update_repo_stats as urs
from scripts.ci_preflight import GRAPH_VALIDATE_ARGS


def _flag_values(args: tuple[str, ...] | list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("--"):
            i += 1
            continue
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            values[token] = args[i + 1]
            i += 2
        else:
            values[token] = ""
            i += 1
    return values


def _workflow_graph_validate_args(path: str, step_name: str) -> dict[str, str]:
    workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    matches: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == step_name:
                matches.append(step["run"])
    assert len(matches) == 1
    command = " ".join(
        line.strip().rstrip("\\") for line in matches[0].splitlines() if line.strip()
    )
    argv = command.split()
    script_index = argv.index("src/validate_graph_artifacts.py")
    return _flag_values(argv[script_index + 1 :])


def _workflow_job_steps(path: str, job_name: str) -> list[dict[str, object]]:
    workflow = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    steps = workflow["jobs"][job_name]["steps"]
    assert isinstance(steps, list)
    return steps


def _graph_contract_counts() -> dict[str, int]:
    flags = _flag_values(GRAPH_VALIDATE_ARGS[1:])
    return {
        "nodes": int(flags["--expected-nodes"]),
        "edges": int(flags["--expected-edges"]),
        "semantic_edges": int(flags["--expected-semantic-edges"]),
        "skill_pages": int(flags["--expected-skill-pages"]),
        "agents": int(flags["--expected-agent-pages"]),
        "mcps": int(flags["--expected-mcp-pages"]),
        "harnesses": int(flags["--expected-harness-pages"]),
        "body_backed_skills": int(flags["--expected-skills-sh-converted"]),
    }


def test_graph_validation_counts_match_preflight_contract() -> None:
    expected = _flag_values(GRAPH_VALIDATE_ARGS[1:])

    assert (
        _workflow_graph_validate_args(
            ".github/workflows/test.yml",
            "Validate shipped graph artifacts",
        )
        == expected
    )
    assert (
        _workflow_graph_validate_args(
            ".github/workflows/publish.yml",
            "Validate release graph artifacts",
        )
        == expected
    )


def test_pr_graph_check_runs_repo_stats_after_artifact_validation() -> None:
    steps = _workflow_job_steps(".github/workflows/test.yml", "graph-check")
    step_names = [str(step.get("name", "")) for step in steps]

    setup_index = step_names.index("Set up Python")
    install_index = step_names.index("Install graph check dependencies")
    artifact_index = step_names.index("Validate shipped graph artifacts")
    stats_index = step_names.index("Validate README and docs stats")

    assert setup_index < stats_index
    assert install_index < stats_index
    assert 'python -m pip install ".[dev]"' in str(steps[install_index]["run"])
    assert stats_index > artifact_index
    assert steps[stats_index]["run"] == "python src/update_repo_stats.py --check"


def test_public_docs_and_readmes_expose_current_graph_counts() -> None:
    counts = _graph_contract_counts()
    core_nodes = counts["nodes"] - counts["body_backed_skills"]
    curated_skills = counts["skill_pages"] - counts["body_backed_skills"]
    skill_pages = f"{counts['skill_pages']:,}"
    mcps = f"{counts['mcps']:,}"

    readme = Path("README.md").read_text(encoding="utf-8")
    assert f"Skills-{quote(skill_pages)}" in readme
    assert f"Agents-{counts['agents']}" in readme
    assert f"MCPs-{quote(mcps)}" in readme
    assert f"Harnesses-{counts['harnesses']}" in readme
    assert f"**{counts['nodes']:,}-node** graph" in readme
    assert f"**{counts['skill_pages']:,} skill entity pages**" in readme
    assert f"**{counts['agents']:,} agents**" in readme
    assert f"**{counts['mcps']:,} MCP servers**" in readme
    assert f"**{counts['harnesses']:,} harnesses**" in readme

    docs_index = Path("docs/index.md").read_text(encoding="utf-8")
    assert f"**{counts['skill_pages']:,} skill pages" in docs_index
    assert f"{counts['agents']:,} agents" in docs_index
    assert f"{counts['mcps']:,} MCP servers" in docs_index
    assert f"{counts['harnesses']:,} cataloged harnesses**" in docs_index
    assert f"**{counts['nodes']:,} graph nodes**" in docs_index
    assert f"({counts['nodes']:,} nodes, {counts['edges']:,} edges)" in docs_index
    assert f"{core_nodes:,}-node core plus {counts['body_backed_skills']:,}" in docs_index

    knowledge_graph = Path("docs/knowledge-graph.md").read_text(encoding="utf-8")
    derived = urs._GRAPH_DERIVED_STATS
    communities_payload = json.loads(Path("graph/communities.json").read_text(encoding="utf-8"))
    communities = int(
        communities_payload.get("total_communities")
        or len(communities_payload.get("communities", []))
    )
    assert f"is **{core_nodes:,} nodes**" in knowledge_graph
    assert f"{curated_skills:,} curated skills" in knowledge_graph
    assert f"| Total nodes | **{counts['nodes']:,}** |" in knowledge_graph
    assert f"| Total edges | **{counts['edges']:,}** |" in knowledge_graph
    assert (
        f"| Hydrated skill incident edges | **{derived['hydrated_incident_edges']:,}** |"
        in knowledge_graph
    )
    assert (
        "| Hydrated skill semantic incident edges | "
        f"**{derived['hydrated_semantic_incident_edges']:,}** |" in knowledge_graph
    )
    assert f"| Communities | **{communities:,}** (Louvain) |" in knowledge_graph
    assert f"semantic {counts['semantic_edges']:,}" in knowledge_graph
    assert f"tag {derived['tag_edges']:,}" in knowledge_graph
    assert f"token {derived['token_edges']:,}" in knowledge_graph
    assert (
        f"| Cross-type edges (skill <-> agent) | ~{derived['cross_skill_agent_edges']:,} |"
        in knowledge_graph
    )
    assert (
        f"| Cross-type edges (skill <-> MCP) | ~{derived['cross_skill_mcp_edges']:,} |"
        in knowledge_graph
    )
    assert (
        f"| Cross-type edges (agent <-> MCP) | ~{derived['cross_agent_mcp_edges']:,} |"
        in knowledge_graph
    )
    assert f"| Harness edges | **{derived['harness_edges']:,}** |" in knowledge_graph
    assert (
        f"**{communities:,} Louvain communities**, "
        f"**{counts['semantic_edges']:,} semantic edges**, "
        f"**{derived['tag_edges']:,} tag edges**" in knowledge_graph
    )

    graph_readme = Path("graph/README.md").read_text(encoding="utf-8")
    expected_flags = _flag_values(GRAPH_VALIDATE_ARGS[1:])
    for flag, value in expected_flags.items():
        if flag.startswith("--expected-"):
            assert f"{flag} {value}" in graph_readme
