from __future__ import annotations

import json
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

import import_skills_sh_catalog as importer
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


def test_update_wiki_tarball_adds_skills_sh_as_first_class_skill_nodes_and_pages(
    tmp_path: Path,
) -> None:
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
                "type": "skill",
                "status": "remote-cataloged",
                "source_catalog": "skills.sh",
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
        page_name = "./entities/skills/skills-sh-open-feishu-cn-lark-doc.md"
        assert page_name in names
        page_member = tf.getmember(page_name)
        page_file = tf.extractfile(page_member)
        assert page_file is not None
        page = page_file.read().decode("utf-8")

    external_node = next(
        node for node in graph_out["nodes"]
        if node["id"] == "skill:skills-sh-open-feishu-cn-lark-doc"
    )
    assert external_node["type"] == "skill"
    assert external_node["status"] == "remote-cataloged"
    assert external_node["source_catalog"] == "skills.sh"
    assert external_node["duplicate_of"] == "skill:lark-doc"
    assert graph_out["graph"]["source_catalog_nodes"]["skills.sh"] == 1
    assert catalog_out["skills"][0]["graph_node_id"] == external_node["id"]
    assert catalog_out["skills"][0]["entity_path"] == page_name.removeprefix("./")
    assert catalog_out["skills"][0]["quality_signals"]["security_review"] == "metadata-only"
    assert catalog_out["body_available_count"] == 0
    assert "body_available: false" in page
    assert "Security review: metadata-only" in page


def test_extract_skill_body_from_skills_sh_detail_html() -> None:
    html = """
    <html>
      <body>
        <main>
          <div class="prose dark:prose-invert">
            <h1>Find Skills</h1>
            <p>Searches for useful agent skills.</p>
            <br>
            <img src="/icon.png" alt="decorative">
            <pre><code>npx skills find "security review"</code></pre>
          </div>
          <aside>unrelated recommendations</aside>
        </main>
      </body>
    </html>
    """

    body = importer._extract_skill_body_from_detail_html(html)

    assert "Find Skills" in body
    assert "Searches for useful agent skills." in body
    assert 'npx skills find "security review"' in body
    assert "unrelated recommendations" not in body
    assert "<h1>" not in body


def test_hydrated_skills_sh_body_is_indexed_and_rendered(
    monkeypatch,
    tmp_path: Path,
) -> None:
    detail_html = """
    <main>
      <div class="prose">
        <h1>Find Skills</h1>
        <p>Use this skill to discover relevant skills.</p>
      </div>
    </main>
    """
    monkeypatch.setattr(
        importer,
        "_fetch_detail_html",
        lambda url, timeout=30, max_bytes=2_000_000: (detail_html, None),
    )
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
        "overlap": {"existing_wiki_skill_pages": 0},
        "skills": [
            {
                "id": "vercel-labs/skills/find-skills",
                "ctx_slug": "skills-sh-vercel-labs-skills-find-skills",
                "source": "vercel-labs/skills",
                "skill_id": "find-skills",
                "name": "find-skills",
                "type": "skill",
                "status": "remote-cataloged",
                "source_catalog": "skills.sh",
                "installs": 100,
                "tags": ["skill"],
                "detail_url": "https://skills.sh/vercel-labs/skills/find-skills",
                "install_command": (
                    "npx skills add https://github.com/vercel-labs/skills "
                    "--skill find-skills"
                ),
            }
        ],
    }
    summary = importer.hydrate_catalog_bodies(
        catalog,
        workers=1,
        limit=1,
        delay_seconds=0,
    )

    assert summary["body_hydrated_count"] == 1
    skill = catalog["skills"][0]
    assert skill["body_available"] is True
    assert skill["body_source_url"] == "https://skills.sh/vercel-labs/skills/find-skills"
    assert "# Find Skills" in skill["skill_body"]

    tarball = tmp_path / "wiki-graph.tar.gz"
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [],
        "edges": [],
    }
    with tarfile.open(tarball, "w:gz") as tf:
        _add_text(tf, "./graphify-out/graph.json", json.dumps(graph))

    update_wiki_tarball(tarball, catalog)

    with tarfile.open(tarball, "r:gz") as tf:
        graph_out = _read_json(tf, "./graphify-out/graph.json")
        catalog_out = _read_json(tf, "./external-catalogs/skills-sh/catalog.json")
        page_member = tf.getmember(
            "./entities/skills/skills-sh-vercel-labs-skills-find-skills.md"
        )
        page_file = tf.extractfile(page_member)
        assert page_file is not None
        page = page_file.read().decode("utf-8")

    graph_node = graph_out["nodes"][0]
    assert graph_node["quality_signals"]["body_available"] is True
    assert catalog_out["body_hydrated_count"] == 1
    assert catalog_out["skills"][0]["quality_signals"]["body_available"] is True
    assert "body_available: true" in page
    assert "Body availability: hydrated from Skills.sh detail page." in page
    assert "## Upstream SKILL.md" in page


def test_hydration_rejects_non_skills_sh_detail_urls(monkeypatch) -> None:
    def fail_fetch(url: str, timeout: int = 30) -> tuple[str | None, str | None]:
        raise AssertionError(f"unexpected fetch for {url}")

    monkeypatch.setattr(importer, "_fetch_detail_html", fail_fetch)
    catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "poisoned/catalog-entry",
                "ctx_slug": "skills-sh-poisoned-catalog-entry",
                "detail_url": "http://169.254.169.254/latest/meta-data",
            }
        ]
    }

    summary = importer.hydrate_catalog_bodies(catalog, workers=1)

    skill = catalog["skills"][0]
    assert summary["body_hydrated_count"] == 0
    assert summary["body_hydration_error_count"] == 1
    assert skill["body_available"] is False
    assert "refused non-skills.sh detail URL" in skill["body_error"]


def test_fetch_detail_html_rejects_oversized_responses(monkeypatch) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return b"x" * size

    monkeypatch.setattr(importer.urllib.request, "urlopen", lambda req, timeout: FakeResponse())

    html, error = importer._fetch_detail_html(
        "https://skills.sh/vercel-labs/skills/find-skills",
        max_bytes=4,
    )

    assert html is None
    assert error == "detail response exceeded 4 bytes"


def test_hydration_truncates_large_bodies(monkeypatch) -> None:
    detail_html = "<main><div class='prose'><p>" + ("x" * 100) + "</p></div></main>"
    monkeypatch.setattr(
        importer,
        "_fetch_detail_html",
        lambda url, timeout=30, max_bytes=2_000_000: (detail_html, None),
    )
    catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "vercel-labs/skills/find-skills",
                "ctx_slug": "skills-sh-vercel-labs-skills-find-skills",
                "detail_url": "https://skills.sh/vercel-labs/skills/find-skills",
            }
        ]
    }

    summary = importer.hydrate_catalog_bodies(catalog, workers=1, max_body_chars=12)

    skill = catalog["skills"][0]
    assert summary["body_hydrated_count"] == 1
    assert skill["body_available"] is True
    assert skill["body_truncated"] is True
    assert len(skill["skill_body"]) == 12
