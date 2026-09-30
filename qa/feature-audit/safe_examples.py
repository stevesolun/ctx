"""Run safe, isolated graph and telemetry documentation examples.

This audit helper performs no network request, provider/model call, service
activation, or real-home write. All mutable state lives under one temporary
directory that is deleted when the run ends.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np

from ctx.core.graph.vector_index import build_vector_index
from ctx.core.wiki.wiki_packs import write_wiki_overlay_pack
from ctx.telemetry import record_event


def _run(argv: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (
        f"command failed: {argv}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    return result


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ctx-safe-examples-") as raw_root:
        root = Path(raw_root)
        home = root / "home"
        wiki = root / "wiki"
        telemetry = root / "telemetry"
        (home / ".claude").mkdir(parents=True)
        (wiki / "entities/skills").mkdir(parents=True)
        telemetry.mkdir()
        (home / ".claude/skill-system-config.json").write_text(
            json.dumps(
                {
                    "graph": {
                        "edge_weights": {
                            "semantic": 0.0,
                            "tags": 0.5,
                            "slug_tokens": 0.5,
                        }
                    }
                }
            )
        )
        (wiki / "entities/skills/python-review.md").write_text(
            "---\nname: Python Review\ntags: [python, review]\n---\n# Python Review\n"
        )
        (wiki / "entities/skills/python-tests.md").write_text(
            "---\nname: Python Tests\ntags: [python, testing]\n---\n# Python Tests\n"
        )
        new_skill = root / "python-helper.md"
        new_skill.write_text(
            "---\nname: Python Helper\ntags: [python, review]\n---\n# Python Helper\n"
        )
        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "CTX_TELEMETRY_ENABLED": "0",
                "CTX_TELEMETRY_HASH_SALT": "fixture-salt",
            }
        )
        py = sys.executable
        graphify = _run(
            [
                py,
                "-m",
                "ctx.core.wiki.wiki_graphify",
                "--wiki-dir",
                str(wiki),
                "--incremental",
                "--graph-only",
                "--semantic-vector-index",
                "off",
            ],
            env=env,
        )
        graph = json.loads((wiki / "graphify-out/graph.json").read_text())
        node_ids = sorted(node["id"] for node in graph["nodes"])
        assert node_ids == ["skill:python-review", "skill:python-tests"]
        assert len(graph["edges"]) == 1

        base_manifest_path = next(
            (wiki / "graphify-out/packs").glob("base-*/graph-pack-manifest.json")
        )
        base = json.loads(base_manifest_path.read_text())
        index_dir = wiki / ".embedding-cache/graph/vector-index"
        build_vector_index(
            kind="numpy-flat",
            model_id=base["model_id"],
            node_ids=node_ids,
            content_hashes=["fixture-review-hash", "fixture-tests-hash"],
            vectors=np.asarray([[1.0, 0.0], [0.8, 0.6]], dtype=np.float32),
        ).save(index_dir)
        validated_index = json.loads(
            _run(
                [
                    py,
                    "-m",
                    "ctx.core.graph.incremental_attach",
                    "validate-indexes",
                    "--index-dir",
                    str(index_dir),
                    "--json",
                ],
                env=env,
            ).stdout
        )
        assert validated_index["ok"] is True
        assert validated_index["node_count"] == 2

        write_wiki_overlay_pack(
            pack_dir=wiki / "wiki-packs/overlay-safe-example",
            pack_id="overlay-safe-example",
            base_export_id=base["base_export_id"],
            parent_export_id=base["base_export_id"],
            pages={"entities/skills/python-helper.md": new_skill.read_text()},
            tombstones=[],
        )
        attach = json.loads(
            _run(
                [
                    py,
                    "-m",
                    "ctx.core.graph.incremental_attach",
                    "attach",
                    "--index-dir",
                    str(index_dir),
                    "--overlay",
                    str(wiki / "graphify-out/entity-overlays.jsonl"),
                    "--node-id",
                    "skill:python-helper",
                    "--type",
                    "skill",
                    "--label",
                    "python-helper",
                    "--tag",
                    "python",
                    "--tag",
                    "review",
                    "--text-file",
                    str(new_skill),
                    "--vector-json",
                    "[1.0, 0.0]",
                    "--model-id",
                    base["model_id"],
                    "--min-final-weight",
                    "0.0",
                    "--pack-root",
                    str(wiki / "graphify-out/packs"),
                    "--base-export-id",
                    base["base_export_id"],
                    "--parent-export-id",
                    base["base_export_id"],
                    "--config-hash",
                    base["config_hash"],
                    "--json",
                ],
                env=env,
            ).stdout
        )
        assert attach["status"] == "inserted"
        assert [edge["target"] for edge in attach["record"]["edges"]] == node_ids

        stage = root / "pack-stage"
        compacted = json.loads(
            _run(
                [
                    py,
                    "-m",
                    "ctx.core.wiki.pack_compaction",
                    "compact",
                    "--wiki-path",
                    str(wiki),
                    "--base-export-id",
                    "safe-example-compact-v1",
                    "--staging-dir",
                    str(stage),
                    "--json",
                ],
                env=env,
            ).stdout
        )
        validated_packs = json.loads(
            _run(
                [
                    py,
                    "-m",
                    "ctx.core.wiki.pack_compaction",
                    "validate",
                    "--staged-graph-packs-dir",
                    str(stage / "graph-packs"),
                    "--staged-wiki-packs-dir",
                    str(stage / "wiki-packs"),
                    "--require-compaction-manifest",
                    "--json",
                ],
                env=env,
            ).stdout
        )
        assert compacted["graph"]["node_count"] == 3
        assert compacted["wiki"]["page_count"] == 3
        assert validated_packs["missing_wiki_pages"] == 0
        assert validated_packs["orphan_wiki_pages"] == 0

        spool_root = home / ".ctx/telemetry"
        spool_root.mkdir(parents=True)
        spool = spool_root / "events.jsonl"
        sensitive_query = "fixture private request"
        sensitive_token = "fixture sensitive token"
        sensitive_path = str(root / "private-repository")
        config = {
            "mode": "local_redacted",
            "path": str(spool),
            "export": {"enabled": False},
        }
        for name, payload in (
            ("ctx.cli.run", {"query": sensitive_query, "token": sensitive_token}),
            (
                "recommendation.returned",
                {"repo_path": sensitive_path, "result_count": 2},
            ),
        ):
            assert (
                record_event(
                    name,
                    source="ctx-cli",
                    session_id="safe-example-session",
                    payload=payload,
                    path=spool,
                    trusted_root=spool_root,
                    config=config,
                )
                is not None
            )
        exporter = str(Path(py).with_name("ctx-telemetry-export"))
        checkpoint = telemetry / "checkpoint.json"
        common = [
            exporter,
            "--path",
            str(spool),
            "--checkpoint",
            str(checkpoint),
            "--sink",
            "local_jsonl",
        ]
        preview = json.loads(
            _run(
                [
                    *common,
                    "--output",
                    str(telemetry / "preview-unused.jsonl"),
                    "--dry-run",
                    "--json",
                ],
                env=env,
            ).stdout
        )
        incremental_path = telemetry / "incremental.jsonl"
        exported = json.loads(
            _run(
                [*common, "--output", str(incremental_path), "--json"],
                env=env,
            ).stdout
        )
        replay_path = telemetry / "replay.jsonl"
        replayed = json.loads(
            _run(
                [*common, "--all", "--output", str(replay_path), "--json"],
                env=env,
            ).stdout
        )
        spool_ids = [row["event_id"] for row in _jsonl(spool)]
        assert preview["attempted"] == 2 and preview["exported"] == 0
        assert exported["exported"] == 2 and replayed["exported"] == 2
        assert [row["event_id"] for row in _jsonl(incremental_path)] == spool_ids
        assert [row["event_id"] for row in _jsonl(replay_path)] == spool_ids
        for path in (spool, incremental_path, replay_path):
            text = path.read_text()
            assert sensitive_query not in text
            assert sensitive_token not in text
            assert sensitive_path not in text

        print(
            json.dumps(
                {
                    "graphify": graphify.stdout.splitlines()[0],
                    "index_nodes": validated_index["node_count"],
                    "attach_edges": len(attach["record"]["edges"]),
                    "compacted_graph_nodes": compacted["graph"]["node_count"],
                    "compacted_wiki_pages": compacted["wiki"]["page_count"],
                    "telemetry_preview_attempted": preview["attempted"],
                    "telemetry_exported": exported["exported"],
                    "telemetry_replayed": replayed["exported"],
                    "external_calls": 0,
                },
                indent=2,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
