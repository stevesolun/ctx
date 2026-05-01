from __future__ import annotations

import json
import tarfile
import zlib
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


def test_update_wiki_tarball_preserves_existing_skills_sh_semantic_edges(
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
            },
            {
                "id": "skill:skills-sh-open-feishu-cn-lark-doc",
                "label": "skills-sh-open-feishu-cn-lark-doc",
                "type": "skill",
                "tags": ["docs"],
            },
        ],
        "edges": [
            {
                "source": "skill:lark-doc",
                "target": "skill:skills-sh-open-feishu-cn-lark-doc",
                "semantic_sim": 0.91,
                "tag_sim": 0.2,
                "token_sim": 0.0,
                "final_weight": 0.667,
                "weight": 0.667,
                "shared_tags": ["docs"],
                "shared_tokens": [],
            }
        ],
    }
    with tarfile.open(tarball, "w:gz") as tf:
        _add_text(tf, "./graphify-out/graph.json", json.dumps(graph))

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

    node = next(
        node for node in graph_out["nodes"]
        if node["id"] == "skill:skills-sh-open-feishu-cn-lark-doc"
    )
    assert node["source_catalog"] == "skills.sh"
    edges = [
        edge for edge in graph_out["edges"]
        if edge["target"] == "skill:skills-sh-open-feishu-cn-lark-doc"
        or edge["source"] == "skill:skills-sh-open-feishu-cn-lark-doc"
    ]
    assert len(edges) == 1
    assert edges[0]["semantic_sim"] == 0.91
    assert edges[0].get("source_catalog") != "skills.sh"


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


def test_extract_skill_body_prefers_skill_md_panel() -> None:
    html = """
    <main>
      <div class="prose">
        <p>Summary card text.</p>
      </div>
      <div><span>SKILL.md</span></div>
      <div class="prose">
        <h1>Microsoft Foundry Skill</h1>
        <p>Canonical upstream body.</p>
      </div>
    </main>
    """

    body = importer._extract_skill_body_from_detail_html(html)

    assert "# Microsoft Foundry Skill" in body
    assert "Canonical upstream body." in body
    assert "Summary card text." not in body


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
        _add_text(
            tf,
            "./converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md",
            "stale body\n",
        )

    update_wiki_tarball(tarball, catalog)

    with tarfile.open(tarball, "r:gz") as tf:
        names = tf.getnames()
        graph_out = _read_json(tf, "./graphify-out/graph.json")
        catalog_out = _read_json(tf, "./external-catalogs/skills-sh/catalog.json")
        page_member = tf.getmember(
            "./entities/skills/skills-sh-vercel-labs-skills-find-skills.md"
        )
        page_file = tf.extractfile(page_member)
        assert page_file is not None
        page = page_file.read().decode("utf-8")
        converted_member = tf.getmember(
            "./converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md"
        )
        converted_file = tf.extractfile(converted_member)
        assert converted_file is not None
        converted = converted_file.read().decode("utf-8")

    assert names.count("./converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md") == 1
    graph_node = graph_out["nodes"][0]
    assert graph_node["quality_signals"]["body_available"] is True
    assert catalog_out["body_hydrated_count"] == 1
    assert catalog_out["skills"][0]["quality_signals"]["body_available"] is True
    assert catalog_out["skills"][0]["converted_path"] == (
        "converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md"
    )
    assert graph_node["converted_path"] == (
        "converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md"
    )
    assert "skill_body" not in catalog_out["skills"][0]
    assert catalog_out["skills"][0]["body_char_count"] == len(skill["skill_body"])
    assert "body_available: true" in page
    assert (
        "converted_path: "
        '"converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md"'
    ) in page
    assert "Body availability: hydrated from Skills.sh detail page." in page
    assert "## Upstream SKILL.md" not in page
    assert converted == "# Find Skills\n\nUse this skill to discover relevant skills.\n"


def test_update_wiki_tarball_preserves_stripped_catalog_converted_body(
    tmp_path: Path,
) -> None:
    tarball = tmp_path / "wiki-graph.tar.gz"
    graph = {
        "directed": False,
        "multigraph": False,
        "graph": {},
        "nodes": [],
        "edges": [],
    }
    converted_path = "./converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md"
    with tarfile.open(tarball, "w:gz") as tf:
        _add_text(tf, "./graphify-out/graph.json", json.dumps(graph))
        _add_text(tf, converted_path, "# Existing hydrated body\n")

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
                "body_available": True,
                "converted_path": converted_path.removeprefix("./"),
            }
        ],
    }

    update_wiki_tarball(tarball, catalog)

    with tarfile.open(tarball, "r:gz") as tf:
        names = tf.getnames()
        converted_file = tf.extractfile(tf.getmember(converted_path))
        assert converted_file is not None
        converted = converted_file.read().decode("utf-8")
        catalog_out = _read_json(tf, "./external-catalogs/skills-sh/catalog.json")
        page_file = tf.extractfile(
            tf.getmember("./entities/skills/skills-sh-vercel-labs-skills-find-skills.md")
        )
        assert page_file is not None
        page = page_file.read().decode("utf-8")

    assert names.count(converted_path) == 1
    assert converted == "# Existing hydrated body\n"
    assert catalog_out["body_available_count"] == 1
    assert catalog_out["skills"][0]["body_available"] is True
    assert "skill_body" not in catalog_out["skills"][0]
    assert "body_available: true" in page


def test_update_wiki_tarball_downgrades_missing_stripped_body(
    tmp_path: Path,
) -> None:
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
                "body_available": True,
                "converted_path": (
                    "converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md"
                ),
            }
        ],
    }

    update_wiki_tarball(tarball, catalog)

    with tarfile.open(tarball, "r:gz") as tf:
        names = tf.getnames()
        catalog_out = _read_json(tf, "./external-catalogs/skills-sh/catalog.json")
        page_file = tf.extractfile(
            tf.getmember("./entities/skills/skills-sh-vercel-labs-skills-find-skills.md")
        )
        assert page_file is not None
        page = page_file.read().decode("utf-8")

    assert "./converted/skills-sh-vercel-labs-skills-find-skills/SKILL.md" not in names
    assert catalog_out["body_available_count"] == 0
    assert catalog_out["skills"][0]["body_available"] is False
    assert catalog_out["skills"][0]["converted_path"] is None
    assert "body_available: false" in page


def test_update_wiki_tarball_micro_converts_long_skills_sh_body(
    tmp_path: Path,
) -> None:
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

    long_body = "---\nname: long-skill\ndescription: Long skill\n---\n\n"
    long_body += "# Long Skill\n\n" + "\n".join(f"- ensure item {i}" for i in range(190))
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
                "id": "example/skills/long-skill",
                "ctx_slug": "skills-sh-example-skills-long-skill",
                "source": "example/skills",
                "skill_id": "long-skill",
                "name": "long-skill",
                "type": "skill",
                "status": "remote-cataloged",
                "source_catalog": "skills.sh",
                "installs": 100,
                "tags": ["skill"],
                "detail_url": "https://skills.sh/example/skills/long-skill",
                "install_command": (
                    "npx skills add https://github.com/example/skills "
                    "--skill long-skill"
                ),
                "body_available": True,
                "converted_path": "converted/skills-sh-example-skills-long-skill/SKILL.md",
                "skill_body": long_body,
            }
        ],
    }

    update_wiki_tarball(tarball, catalog)

    with tarfile.open(tarball, "r:gz") as tf:
        names = set(tf.getnames())
        skill_file = tf.extractfile(
            tf.getmember("./converted/skills-sh-example-skills-long-skill/SKILL.md")
        )
        assert skill_file is not None
        skill_text = skill_file.read().decode("utf-8")

    assert "./converted/skills-sh-example-skills-long-skill/references/01-scope.md" in names
    assert "./converted/skills-sh-example-skills-long-skill/check-gates.md" in names
    assert "./converted/skills-sh-example-skills-long-skill/SKILL.md.original" not in names
    assert "When this skill triggers, execute the following gated pipeline." in skill_text


def test_hydration_falls_back_to_github_raw_skill_md(monkeypatch) -> None:
    monkeypatch.setattr(
        importer,
        "_fetch_detail_html",
        lambda url, timeout=30, max_bytes=2_000_000: (
            "<main><span>SKILL.md</span><p>No SKILL.md available.</p></main>",
            None,
        ),
    )
    fetched_urls: list[str] = []

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            return b"# Brand Identity\n\nUse this skill for brand strategy.\n"

    def fake_urlopen(req: object, timeout: int) -> FakeResponse:
        url = req.full_url  # type: ignore[attr-defined]
        fetched_urls.append(url)
        if "/skills/brand-identity/" not in url:
            raise OSError("not found")
        return FakeResponse()

    monkeypatch.setattr(importer.urllib.request, "urlopen", fake_urlopen)
    catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "travisjneuman/.claude/brand-identity",
                "ctx_slug": "skills-sh-travisjneuman-claude-brand-identity",
                "source": "travisjneuman/.claude",
                "skill_id": "brand-identity",
                "detail_url": "https://skills.sh/travisjneuman/.claude/brand-identity",
            }
        ]
    }

    summary = importer.hydrate_catalog_bodies(catalog, workers=1)

    skill = catalog["skills"][0]
    assert summary["body_hydrated_count"] == 1
    assert skill["body_available"] is True
    assert skill["skill_body"].startswith("# Brand Identity")
    assert skill["body_source_url"] == (
        "https://raw.githubusercontent.com/travisjneuman/.claude/"
        "main/skills/brand-identity/SKILL.md"
    )
    assert fetched_urls[-1] == skill["body_source_url"]


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


def test_hydration_checkpoint_resumes_hydrated_bodies(
    monkeypatch,
    tmp_path: Path,
) -> None:
    checkpoint = tmp_path / "hydrate.jsonl.gz"
    detail_html = "<main><div class='prose'><p>Fetched body.</p></div></main>"
    monkeypatch.setattr(
        importer,
        "_fetch_detail_html",
        lambda url, timeout=30, max_bytes=2_000_000: (detail_html, None),
    )
    first_catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "vercel-labs/skills/find-skills",
                "ctx_slug": "skills-sh-vercel-labs-skills-find-skills",
                "detail_url": "https://skills.sh/vercel-labs/skills/find-skills",
            }
        ]
    }

    importer.hydrate_catalog_bodies(
        first_catalog,
        workers=1,
        checkpoint_path=checkpoint,
    )

    fetched_urls: list[str] = []

    def fetch_second(url: str, timeout: int = 30, max_bytes: int = 2_000_000) -> tuple[str, None]:
        fetched_urls.append(url)
        return "<main><div class='prose'><p>Second body.</p></div></main>", None

    monkeypatch.setattr(importer, "_fetch_detail_html", fetch_second)
    resumed_catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "vercel-labs/skills/find-skills",
                "ctx_slug": "skills-sh-vercel-labs-skills-find-skills",
                "detail_url": "https://skills.sh/vercel-labs/skills/find-skills",
            },
            {
                "id": "microsoft/azure-skills/microsoft-foundry",
                "ctx_slug": "skills-sh-microsoft-azure-skills-microsoft-foundry",
                "detail_url": "https://skills.sh/microsoft/azure-skills/microsoft-foundry",
            },
        ]
    }

    summary = importer.hydrate_catalog_bodies(
        resumed_catalog,
        workers=1,
        checkpoint_path=checkpoint,
    )

    assert summary["body_hydration_checkpoint_applied_count"] == 1
    assert summary["body_hydration_attempted_count"] == 1
    assert fetched_urls == ["https://skills.sh/microsoft/azure-skills/microsoft-foundry"]
    assert resumed_catalog["skills"][0]["skill_body"] == "Fetched body."
    assert resumed_catalog["skills"][1]["skill_body"] == "Second body."


def test_hydration_checkpoint_reads_incomplete_gzip_member(tmp_path: Path) -> None:
    checkpoint = tmp_path / "hydrate.jsonl.gz"
    record = {
        "id": "vercel-labs/skills/find-skills",
        "ctx_slug": "skills-sh-vercel-labs-skills-find-skills",
        "skill_body": "Recovered body.",
        "body_available": True,
    }
    compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
    checkpoint.write_bytes(
        compressor.compress((json.dumps(record) + "\n").encode("utf-8"))
        + compressor.flush(zlib.Z_SYNC_FLUSH)
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

    summary = importer.hydrate_catalog_bodies(
        catalog,
        workers=1,
        checkpoint_path=checkpoint,
    )

    assert summary["body_hydration_checkpoint_applied_count"] == 1
    assert summary["body_hydration_attempted_count"] == 0
    assert catalog["skills"][0]["skill_body"] == "Recovered body."


def test_hydration_writes_progress_and_status_file(
    capsys,
    monkeypatch,
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "hydrate-status.json"

    def fetch(url: str, timeout: int = 30, max_bytes: int = 2_000_000) -> tuple[str | None, str | None]:
        if url.endswith("/bad"):
            return None, "boom"
        return "<main><div class='prose'><p>Body.</p></div></main>", None

    monkeypatch.setattr(importer, "_fetch_detail_html", fetch)
    catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "one/good",
                "ctx_slug": "skills-sh-one-good",
                "detail_url": "https://skills.sh/one/good",
            },
            {
                "id": "two/bad",
                "ctx_slug": "skills-sh-two-bad",
                "detail_url": "https://skills.sh/two/bad",
            },
        ]
    }

    summary = importer.hydrate_catalog_bodies(
        catalog,
        workers=2,
        progress_every=1,
        status_path=status_path,
    )

    output = capsys.readouterr().out
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert "hydrate progress:" in output
    assert "2/2 (100.00%)" in output
    assert summary["body_hydrated_count"] == 1
    assert summary["body_hydration_error_count"] == 1
    assert status["status"] == "completed"
    assert status["total"] == 2
    assert status["overall_completed"] == 2
    assert status["hydrated_new"] == 1
    assert status["errors_new"] == 1


def test_hydration_status_does_not_count_limit_deferred_entries(
    monkeypatch,
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "hydrate-status.json"
    monkeypatch.setattr(
        importer,
        "_fetch_detail_html",
        lambda url, timeout=30, max_bytes=2_000_000: (
            "<main><div class='prose'><p>Body.</p></div></main>",
            None,
        ),
    )
    catalog: dict[str, Any] = {
        "skills": [
            {
                "id": "one/done",
                "ctx_slug": "skills-sh-one-done",
                "body_available": True,
                "converted_path": "converted/skills-sh-one-done/SKILL.md",
                "detail_url": "https://skills.sh/one/done",
            },
            {
                "id": "two/todo",
                "ctx_slug": "skills-sh-two-todo",
                "detail_url": "https://skills.sh/two/todo",
            },
            {
                "id": "three/todo",
                "ctx_slug": "skills-sh-three-todo",
                "detail_url": "https://skills.sh/three/todo",
            },
            {
                "id": "four/todo",
                "ctx_slug": "skills-sh-four-todo",
                "detail_url": "https://skills.sh/four/todo",
            },
        ]
    }

    importer.hydrate_catalog_bodies(
        catalog,
        workers=1,
        limit=1,
        status_path=status_path,
    )

    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["overall_completed"] == 2
    assert status["overall_remaining"] == 2
    assert status["remaining_unhydrated"] == 2
    assert status["deferred_by_limit"] == 2
    assert status["percent"] == 50.0
