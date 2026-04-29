from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

from import_skills_sh_catalog import ExistingWikiIndex, normalize_catalog, update_wiki_tarball


def _add_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    tf.addfile(info, BytesIO(payload))


def _read_json(tf: tarfile.TarFile, name: str) -> dict[str, Any]:
    member = tf.getmember(name)
    f = tf.extractfile(member)
    assert f is not None
    data = json.loads(f.read().decode("utf-8"))
    assert isinstance(data, dict)
    return data


def test_normalize_catalog_resolves_truncated_ctx_slug_collisions(monkeypatch) -> None:
    monkeypatch.setattr(
        "import_skills_sh_catalog._read_site_reported_total",
        lambda: None,
    )
    monkeypatch.setattr("import_skills_sh_catalog._read_sitemap_records", lambda: [])

    long_source = "owner/" + ("a" * 180)
    raw = {
        "skills": [
            {
                "id": f"{long_source}/one",
                "source": long_source,
                "skillId": "first-skill",
                "name": "first-skill",
            },
            {
                "id": f"{long_source}/two",
                "source": long_source,
                "skillId": "second-skill",
                "name": "second-skill",
            },
        ]
    }

    catalog = normalize_catalog(raw, ExistingWikiIndex(skill_slugs=set(), skill_ids=set()))
    slugs = [item["ctx_slug"] for item in catalog["skills"]]

    assert len(slugs) == len(set(slugs))
    assert catalog["ctx_slug_collisions_resolved"] == 1
    assert any(item["overlap"]["ctx_slug_collision_resolved"] for item in catalog["skills"])


def test_update_wiki_tarball_adds_external_skill_nodes_and_pages(tmp_path: Path) -> None:
    tarball = tmp_path / "wiki-graph.tar.gz"
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [
            {
                "id": "skill:lark-doc",
                "label": "lark-doc",
                "type": "skill",
                "tags": ["docs"],
            }
        ],
        "edges": [],
    }
    with tarfile.open(tarball, "w:gz") as tf:
        _add_text(tf, "./graphify-out/graph.json", json.dumps(graph))
        _add_text(tf, "./entities/skills/lark-doc.md", "# lark-doc\n")

    catalog: dict[str, Any] = {
        "schema_version": 1,
        "source": "skills.sh",
        "api": "https://skills.sh/api/search",
        "fetched_at": "2026-04-29T00:00:00+00:00",
        "site_reported_total": 1,
        "observed_unique_skills": 1,
        "coverage_vs_site_reported_total": 1.0,
        "query_count": 1,
        "query_error_count": 0,
        "overlap": {"existing_wiki_skill_pages": 1},
        "skills": [
            {
                "id": "open.feishu.cn/lark-doc",
                "ctx_slug": "skills-sh-open-feishu-cn-lark-doc",
                "source": "open.feishu.cn",
                "skill_id": "lark-doc",
                "name": "lark-doc",
                "type": "external-skill",
                "external_catalog": "skills.sh",
                "installs": 18029,
                "tags": ["docs"],
                "detail_url": "https://skills.sh/site/open.feishu.cn/lark-doc",
                "install_command": "npx skills add https://open.feishu.cn",
            }
        ],
    }

    update_wiki_tarball(tarball, catalog)

    with tarfile.open(tarball, "r:gz") as tf:
        graph_out = _read_json(tf, "./graphify-out/graph.json")
        catalog_out = _read_json(tf, "./external-catalogs/skills-sh/catalog.json")
        names = {member.name for member in tf.getmembers()}
        page_name = "./entities/external-skills/o/skills-sh-open-feishu-cn-lark-doc.md"
        assert page_name in names
        page_member = tf.getmember(page_name)
        page_file = tf.extractfile(page_member)
        assert page_file is not None
        page = page_file.read().decode("utf-8")

    external_node = next(
        node for node in graph_out["nodes"]
        if node["id"] == "external-skill:skills-sh:skills-sh-open-feishu-cn-lark-doc"
    )
    assert external_node["type"] == "external-skill"
    assert external_node["external_catalog"] == "skills.sh"
    assert external_node["duplicate_of"] == "skill:lark-doc"
    assert graph_out["graph"]["external_catalog_nodes"]["skills.sh"] == 1
    assert catalog_out["skills"][0]["graph_node_id"] == external_node["id"]
    assert catalog_out["skills"][0]["entity_path"] == page_name.removeprefix("./")
    assert catalog_out["skills"][0]["quality_signals"]["security_review"] == "metadata-only"
    assert "Security review: metadata-only" in page
