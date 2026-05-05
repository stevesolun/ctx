"""Tests for ctx_monitor — dashboard aggregation and HTML rendering."""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import ctx_monitor as cm
from ctx.core.wiki import wiki_queue


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch) -> Path:
    """Point ctx_monitor at a throwaway ~/.claude tree."""
    claude = tmp_path / ".claude"
    (claude / "skill-quality").mkdir(parents=True)
    monkeypatch.setattr(cm, "_claude_dir", lambda: claude)
    return claude


def _write_audit(claude: Path, records: list[dict]) -> None:
    path = claude / "ctx-audit.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_events(claude: Path, records: list[dict]) -> None:
    path = claude / "skill-events.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )


def _write_sidecar(claude: Path, slug: str, body: dict) -> None:
    (claude / "skill-quality" / f"{slug}.json").write_text(
        json.dumps(body), encoding="utf-8",
    )


def _write_mcp_sidecar(claude: Path, slug: str, body: dict) -> None:
    mcp_dir = claude / "skill-quality" / "mcp"
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / f"{slug}.json").write_text(json.dumps(body), encoding="utf-8")


def test_read_jsonl_skips_non_object_lines(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"event": "ok"}),
            json.dumps(["not", "an", "object"]),
            "not-json",
            json.dumps("scalar"),
        ]),
        encoding="utf-8",
    )

    assert cm._read_jsonl(path) == [{"event": "ok"}]


def test_read_jsonl_limit_keeps_tail_without_full_slice(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "\n".join(json.dumps({"i": i}) for i in range(5)),
        encoding="utf-8",
    )

    assert cm._read_jsonl(path, limit=2) == [{"i": 3}, {"i": 4}]


def _post_json(port: int, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-CTX-Monitor-Token"] = token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_raw(
    port: int,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", path)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        if body:
            conn.send(body)
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        conn.close()


def _serve_monitor(
    monkeypatch: pytest.MonkeyPatch,
    token: str = "test-token",
    host: str = "127.0.0.1",
):
    monkeypatch.setattr(cm, "_MONITOR_TOKEN", token)
    server = cm._make_monitor_server(host, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_port


def test_summarize_sessions_merges_audit_and_events(fake_claude: Path) -> None:
    _write_audit(fake_claude, [
        {"ts": "2026-04-19T10:00:00Z", "event": "skill.loaded",
         "subject_type": "skill", "subject": "python-patterns",
         "actor": "hook", "session_id": "S1"},
        {"ts": "2026-04-19T10:05:00Z", "event": "skill.score_updated",
         "subject_type": "skill", "subject": "python-patterns",
         "actor": "hook", "session_id": "S1"},
        {"ts": "2026-04-19T10:10:00Z", "event": "agent.loaded",
         "subject_type": "agent", "subject": "code-reviewer",
         "actor": "hook", "session_id": "S2"},
    ])
    _write_events(fake_claude, [
        {"timestamp": "2026-04-19T10:01:00Z", "event": "load",
         "skill": "fastapi-pro", "session_id": "S1"},
        {"timestamp": "2026-04-19T10:02:00Z", "event": "unload",
         "skill": "fastapi-pro", "session_id": "S1"},
    ])
    sessions = cm._summarize_sessions()
    by_id = {s["session_id"]: s for s in sessions}
    assert "S1" in by_id
    assert "S2" in by_id
    assert "python-patterns" in by_id["S1"]["skills_loaded"]
    assert "fastapi-pro" in by_id["S1"]["skills_loaded"]
    assert "fastapi-pro" in by_id["S1"]["skills_unloaded"]
    assert by_id["S1"]["score_updates"] == 1
    assert "code-reviewer" in by_id["S2"]["agents_loaded"]


def test_grade_distribution(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "a", {"slug": "a", "grade": "A", "raw_score": 0.9})
    _write_sidecar(fake_claude, "b1", {"slug": "b1", "grade": "B", "raw_score": 0.7})
    _write_sidecar(fake_claude, "b2", {"slug": "b2", "grade": "B", "raw_score": 0.6})
    _write_sidecar(fake_claude, "f", {"slug": "f", "grade": "F", "raw_score": 0.1})
    _write_mcp_sidecar(fake_claude, "mcp-one", {
        "slug": "mcp-one", "subject_type": "mcp-server",
        "grade": "C", "raw_score": 0.5,
    })
    dist = cm._grade_distribution()
    assert dist["A"] == 1
    assert dist["B"] == 2
    assert dist["C"] == 1
    assert dist["F"] == 1


def test_grade_distribution_skips_dotfiles_and_lifecycle(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "real", {"slug": "real", "grade": "C", "raw_score": 0.4})
    (fake_claude / "skill-quality" / ".hook-state.json").write_text("{}",
                                                                     encoding="utf-8")
    (fake_claude / "skill-quality" / "real.lifecycle.json").write_text("{}",
                                                                        encoding="utf-8")
    dist = cm._grade_distribution()
    assert sum(dist.values()) == 1  # only "real.json"


def test_session_detail_filters_by_session_id(fake_claude: Path) -> None:
    _write_audit(fake_claude, [
        {"ts": "t1", "event": "skill.loaded",
         "subject_type": "skill", "subject": "x",
         "actor": "hook", "session_id": "A"},
        {"ts": "t2", "event": "skill.loaded",
         "subject_type": "skill", "subject": "y",
         "actor": "hook", "session_id": "B"},
    ])
    _write_events(fake_claude, [
        {"timestamp": "t3", "event": "load", "skill": "z", "session_id": "A"},
    ])
    detail = cm._session_detail("A")
    assert detail["session_id"] == "A"
    assert len(detail["audit_entries"]) == 1
    assert detail["audit_entries"][0]["subject"] == "x"
    assert len(detail["load_events"]) == 1
    assert detail["load_events"][0]["skill"] == "z"


def test_render_home_has_grade_pills(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "s1", {"slug": "s1", "grade": "A", "raw_score": 0.9})
    html = cm._render_home()
    assert "ctx monitor" in html
    assert "grade-A" in html
    assert "/sessions" in html


def test_render_session_detail_escapes_html(fake_claude: Path) -> None:
    hostile = "evil</script><script>alert(1)</script>"
    _write_audit(fake_claude, [
        {"ts": "t", "event": "skill.loaded",
         "subject_type": "skill", "subject": hostile,
         "actor": "hook", "session_id": "sess"},
    ])
    html = cm._render_session_detail("sess")
    assert "<script>alert(1)</script>" not in html
    # HTML-escaped form must appear
    assert "&lt;/script&gt;" in html or "&lt;script&gt;" in html


def test_render_skills_sorts_grade_then_score(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "low", {"slug": "low", "grade": "D", "raw_score": 0.2})
    _write_sidecar(fake_claude, "mid", {"slug": "mid", "grade": "B", "raw_score": 0.6})
    _write_sidecar(fake_claude, "top", {"slug": "top", "grade": "A", "raw_score": 0.9})
    html = cm._render_skills()
    # 'top' should appear before 'mid' before 'low' in the grade-sorted output
    idx_top = html.index("top</code>")
    idx_mid = html.index("mid</code>")
    idx_low = html.index("low</code>")
    assert idx_top < idx_mid < idx_low


def test_render_skills_includes_harness_filter_and_typed_links(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "langgraph-harness", {
        "slug": "langgraph",
        "subject_type": "harness",
        "grade": "A",
        "raw_score": 0.95,
    })

    html = cm._render_skills()

    assert "class='type-filter' value='harness'" in html
    assert "/skill/langgraph?type=harness" in html
    assert "/wiki/langgraph?type=harness" in html
    assert "/graph?slug=langgraph&amp;type=harness" in html


def test_read_manifest_empty_when_missing(fake_claude: Path) -> None:
    m = cm._read_manifest()
    assert m == {"load": [], "unload": [], "warnings": []}


def test_read_manifest_reads_real_manifest(fake_claude: Path) -> None:
    (fake_claude / "skill-manifest.json").write_text(
        json.dumps({"load": [{"skill": "a"}], "unload": [{"skill": "b"}],
                    "warnings": []}),
        encoding="utf-8",
    )
    m = cm._read_manifest()
    assert [e["skill"] for e in m["load"]] == ["a"]
    assert [e["skill"] for e in m["unload"]] == ["b"]


def test_read_manifest_includes_installed_harness_records(fake_claude: Path) -> None:
    harness_dir = fake_claude / "harness-installs"
    harness_dir.mkdir()
    (harness_dir / "langgraph.json").write_text(
        json.dumps({
            "slug": "langgraph",
            "status": "installed",
            "repo_url": "https://github.com/langchain-ai/langgraph",
            "target": str(fake_claude / "harnesses" / "langgraph"),
            "installed_at": "2026-05-01T00:00:00Z",
        }),
        encoding="utf-8",
    )

    m = cm._read_manifest()

    assert m["load"] == [{
        "skill": "langgraph",
        "entity_type": "harness",
        "source": "ctx-harness-install",
        "command": str(fake_claude / "harnesses" / "langgraph"),
        "installed_at": "2026-05-01T00:00:00Z",
        "status": "installed",
    }]


def test_queue_status_summarizes_worker_jobs(fake_claude: Path) -> None:
    wiki = fake_claude / "skill-wiki"
    db_path = wiki_queue.queue_db_path(wiki)
    first = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"graph_only": True},
        source="test",
        now=10.0,
    )
    second = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.TAR_REFRESH_JOB,
        payload={"catalog": "graph/skills-sh-catalog.json.gz"},
        source="test",
        now=11.0,
    )
    leased = wiki_queue.lease_next(db_path, worker_id="worker-a", now=12.0)
    assert leased is not None
    wiki_queue.mark_failed(db_path, leased.id, error="boom", retry=False, now=13.0)

    status = cm._queue_status()

    assert status["available"] is True
    assert status["counts"] == {
        wiki_queue.STATUS_PENDING: 1,
        wiki_queue.STATUS_RUNNING: 0,
        wiki_queue.STATUS_SUCCEEDED: 0,
        wiki_queue.STATUS_FAILED: 1,
    }
    assert status["total"] == 2
    assert [job["id"] for job in status["recent_jobs"]] == [second.id, first.id]
    assert status["recent_jobs"][0]["kind"] == wiki_queue.TAR_REFRESH_JOB


def test_artifact_status_reads_promotion_metadata(fake_claude: Path) -> None:
    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph = graph_dir / "graph.json"
    graph.write_text('{"nodes":[],"edges":[]}', encoding="utf-8")
    (graph_dir / "graph.json.promotion.json").write_text(
        json.dumps({
            "status": "promoted",
            "target": str(graph),
            "previous": {"sha256": "old", "size": 10},
            "current": {"sha256": "new", "size": 22},
            "promoted_at": "2026-05-04T00:00:00+00:00",
        }),
        encoding="utf-8",
    )

    status = cm._artifact_status()

    assert status["graph_json"]["exists"] is True
    assert status["graph_json"]["size"] == graph.stat().st_size
    assert status["promotion_count"] == 1
    assert status["promotions"][0]["status"] == "promoted"
    assert status["promotions"][0]["current_sha256"] == "new"


def test_status_page_and_api_show_queue_and_artifacts(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wiki = fake_claude / "skill-wiki"
    wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.GRAPH_EXPORT_JOB,
        payload={"graph_only": True},
        source="test",
        now=10.0,
    )
    html_out = cm._render_status()
    assert "Queue state" in html_out
    assert "Artifact versions" in html_out
    assert wiki_queue.GRAPH_EXPORT_JOB in html_out

    server, _thread, port = _serve_monitor(monkeypatch)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/status.json",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        assert payload["queue"]["total"] == 1
        assert payload["artifacts"]["graph_json"]["path"].endswith("graph.json")
    finally:
        server.shutdown()
        server.server_close()


def test_render_loaded_shows_manifest_entries(fake_claude: Path) -> None:
    (fake_claude / "skill-manifest.json").write_text(
        json.dumps({
            "load": [{"skill": "python-patterns", "source": "user-approved",
                      "priority": 7, "reason": "fuzzy match"}],
            "unload": [{"skill": "old-skill", "source": "stale"}],
            "warnings": [],
        }),
        encoding="utf-8",
    )
    html = cm._render_loaded()
    assert "python-patterns" in html
    assert "old-skill" in html
    # Action buttons must be present for each row.
    assert "btn-unload" in html
    assert "btn-load" in html
    # Navigation must include new pages.
    assert "/loaded" in html
    assert "/logs" in html


def test_render_loaded_shows_harness_install_without_unload_button(fake_claude: Path) -> None:
    harness_dir = fake_claude / "harness-installs"
    harness_dir.mkdir()
    (harness_dir / "langgraph.json").write_text(
        json.dumps({"slug": "langgraph", "status": "installed"}),
        encoding="utf-8",
    )

    html = cm._render_loaded()

    assert "langgraph" in html
    assert "ctx-harness-install langgraph --uninstall --dry-run" in html
    assert "data-slug='langgraph'" not in html


def test_render_logs_filters_and_renders(fake_claude: Path) -> None:
    _write_audit(fake_claude, [
        {"ts": "t1", "event": "skill.loaded", "subject_type": "skill",
         "subject": "s1", "actor": "hook", "session_id": "sess"},
        {"ts": "t2", "event": "skill.score_updated", "subject_type": "skill",
         "subject": "s1", "actor": "hook", "session_id": "sess"},
    ])
    html = cm._render_logs()
    assert "skill.loaded" in html
    assert "skill.score_updated" in html
    # Filter input must be wired.
    assert "id='filter'" in html


def test_perform_load_rejects_invalid_slug() -> None:
    ok, msg = cm._perform_load("../etc/passwd")
    assert ok is False
    assert "invalid slug" in msg


def test_perform_unload_rejects_invalid_slug() -> None:
    ok, msg = cm._perform_unload("../../hostile")
    assert ok is False
    assert "invalid slug" in msg


def test_load_sidecar_rejects_unsafe_slug(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "python-patterns", {"slug": "python-patterns"})
    assert cm._load_sidecar("../python-patterns") is None


def test_load_sidecar_reads_mcp_quality_subdir(fake_claude: Path) -> None:
    _write_mcp_sidecar(fake_claude, "filesystem", {
        "slug": "filesystem",
        "grade": "A",
        "raw_score": 0.91,
    })
    sidecar = cm._load_sidecar("filesystem")
    assert sidecar is not None
    assert sidecar["subject_type"] == "mcp-server"


def test_load_sidecar_can_disambiguate_duplicate_slug(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "langgraph", {
        "slug": "langgraph",
        "subject_type": "skill",
        "grade": "D",
    })
    harness_sidecar = {
        "slug": "langgraph",
        "subject_type": "harness",
        "grade": "A",
    }
    # Dashboard sidecars are flat for non-MCP entity types; duplicate slugs
    # are disambiguated by the subject_type inside the sidecar.
    (fake_claude / "skill-quality" / "langgraph-harness.json").write_text(
        json.dumps(harness_sidecar), encoding="utf-8",
    )

    skill_sidecar = cm._load_sidecar("langgraph", entity_type="skill")
    assert skill_sidecar is not None
    assert skill_sidecar["grade"] == "D"
    harness = cm._load_sidecar("langgraph", entity_type="harness")
    assert harness is not None
    assert harness["grade"] == "A"


def test_monitor_post_requires_token(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, body = _post_json(port, "/api/load", {"slug": "python-patterns"})
        assert status == 403
        assert "token" in body["detail"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_monitor_post_accepts_valid_token(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_load(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
        calls.append((slug, entity_type))
        return True, "loaded"

    monkeypatch.setattr(cm, "_perform_load", fake_load)
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, body = _post_json(
            port,
            "/api/load",
            {"slug": "python-patterns", "entity_type": "agent"},
            token="test-token",
        )
        assert status == 200
        assert body == {"ok": True, "detail": "loaded"}
        assert calls == [("python-patterns", "agent")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_monitor_post_rejects_cross_origin_with_valid_token(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_load(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
        calls.append(slug)
        return True, f"loaded {entity_type}"

    monkeypatch.setattr(cm, "_perform_load", fake_load)
    server, thread, port = _serve_monitor(monkeypatch)
    body = json.dumps({"slug": "python-patterns"}).encode("utf-8")
    try:
        status, payload = _post_raw(
            port,
            "/api/load",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-CTX-Monitor-Token": "test-token",
                "Origin": "http://evil.example",
            },
            body=body,
        )
        assert status == 403
        assert "cross-origin" in payload["detail"]
        assert calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    ("length", "status", "detail"),
    [
        ("nope", 400, "invalid Content-Length"),
        ("-1", 400, "invalid Content-Length"),
        (str(cm._MAX_POST_BODY_BYTES + 1), 413, "too large"),
    ],
)
def test_monitor_post_rejects_bad_content_length_before_body_read(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
    length: str,
    status: int,
    detail: str,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        code, payload = _post_raw(
            port,
            "/api/load",
            headers={
                "Content-Type": "application/json",
                "Content-Length": length,
                "X-CTX-Monitor-Token": "test-token",
            },
        )
        assert code == status
        assert detail in payload["detail"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_monitor_post_rejects_non_object_json_body(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    body = b"[]"
    try:
        status, payload = _post_raw(
            port,
            "/api/load",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "X-CTX-Monitor-Token": "test-token",
            },
            body=body,
        )
        assert status == 400
        assert "object" in payload["detail"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_monitor_non_loopback_bind_is_read_only(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_load(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
        calls.append(slug)
        return True, f"loaded {entity_type}"

    (fake_claude / "skill-manifest.json").write_text(
        json.dumps({"load": [], "unload": [], "warnings": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cm, "_perform_load", fake_load)
    server, thread, port = _serve_monitor(
        monkeypatch,
        token="browser-token",
        host="0.0.0.0",
    )
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/loaded", timeout=5) as response:
            loaded_html = response.read().decode("utf-8")
        assert "browser-token" not in loaded_html
        assert "Read-only mode" in loaded_html

        status, body = _post_json(
            port,
            "/api/load",
            {"slug": "python-patterns"},
            token="browser-token",
        )
        assert status == 403
        assert "disabled" in body["detail"]
        assert calls == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_host_allows_mutations_only_for_loopback() -> None:
    assert cm._host_allows_mutations("127.0.0.1")
    assert cm._host_allows_mutations("::1")
    assert cm._host_allows_mutations("localhost")
    assert not cm._host_allows_mutations("0.0.0.0")
    assert not cm._host_allows_mutations("::")
    assert not cm._host_allows_mutations("example.com")


def test_graph_api_invalid_params_return_400(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/graph/langgraph.json?hops=abc",
                timeout=5,
            )
        assert excinfo.value.code == 400
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert body["detail"] == "hops and limit must be integers"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_parse_frontmatter_basic() -> None:
    text = "---\nname: python-patterns\nuse_count: 3\n---\n# Body\n\nhello"
    meta, body = cm._parse_frontmatter(text)
    assert meta == {"name": "python-patterns", "use_count": "3"}
    assert body.startswith("# Body")


def test_parse_frontmatter_missing_returns_empty_meta() -> None:
    text = "# No frontmatter\n\nBody only."
    meta, body = cm._parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_multiline_tags() -> None:
    text = "---\ntags:\n  - python\n  - api\n---\n# Body\n"
    meta, body = cm._parse_frontmatter(text)
    assert meta["tags"] == ["python", "api"]
    assert body == "# Body"


def test_wiki_entity_path_rejects_unsafe_slug(fake_claude: Path) -> None:
    assert cm._wiki_entity_path("../../etc/passwd") is None
    assert cm._wiki_entity_path("path/with/slash") is None
    assert cm._wiki_entity_path("con.txt") is None
    # Absent slug returns None but doesn't raise.
    assert cm._wiki_entity_path("no-such-skill") is None


@pytest.mark.parametrize("slug", ["python-patterns", "mcp.v2", "0-service"])
def test_monitor_slug_validator_accepts_safe_values(slug: str) -> None:
    assert cm._is_safe_slug(slug)


@pytest.mark.parametrize("slug", ["con.txt", "nul.", "COM1", "LPT9.ini"])
def test_monitor_slug_validator_rejects_windows_reserved_names(slug: str) -> None:
    assert not cm._is_safe_slug(slug)


def test_monitor_sse_stream_does_not_block_json_requests(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_audit(fake_claude, [
        {"ts": "t", "event": "skill.loaded", "subject": "python-patterns",
         "session_id": "s1"},
    ])
    server, thread, port = _serve_monitor(monkeypatch)
    stream = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/events.stream", timeout=2
    )
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/sessions.json", timeout=2,
        ) as response:
            assert response.status == 200
            body = json.loads(response.read().decode("utf-8"))
        assert body[0]["session_id"] == "s1"
    finally:
        stream.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_monitor_shutdown_signals_open_sse_workers(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    stream = urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/events.stream", timeout=2
    )
    try:
        server.shutdown()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert server._ctx_shutdown.is_set()
    finally:
        stream.close()
        server.server_close()


def test_wiki_entity_path_finds_skill_page(fake_claude: Path) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skills_dir.mkdir(parents=True)
    target = skills_dir / "python-patterns.md"
    target.write_text("---\nname: python-patterns\n---\n# body\n",
                      encoding="utf-8")
    assert cm._wiki_entity_path("python-patterns") == target


def test_render_wiki_entity_with_real_page(fake_claude: Path) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "python-patterns.md").write_text(
        "---\nname: python-patterns\nuse_count: 2\n---\n# Python patterns\n\nBody text.",
        encoding="utf-8",
    )
    html_out = cm._render_wiki_entity("python-patterns")
    assert "python-patterns" in html_out
    assert "Python patterns" in html_out or "Body text" in html_out
    assert "use_count" in html_out  # frontmatter table


def test_render_wiki_entity_missing_slug(fake_claude: Path) -> None:
    out = cm._render_wiki_entity("nope-not-here")
    assert "No wiki page" in out


def test_render_graph_emits_cytoscape_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cm,
        "_graph_stats",
        lambda: {"nodes": 0, "edges": 0, "available": False},
    )
    html_out = cm._render_graph("python-patterns")
    assert "id='cy'" in html_out
    assert "cytoscape" in html_out
    # Initial slug must be embedded as JSON literal so the JS picks it up.
    assert "\"python-patterns\"" in html_out


def test_render_graph_focus_controls_preserve_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cm,
        "_graph_stats",
        lambda: {"nodes": 0, "edges": 0, "available": False},
    )
    html_out = cm._render_graph("langgraph", focus_type="harness")
    assert "id='focus-type'" in html_out
    assert "<option value='harness' selected>harness</option>" in html_out
    assert "load(document.getElementById('focus').value.trim(), selectedFocusType())" in html_out


def test_graph_neighborhood_rejects_unsafe_slug() -> None:
    result = cm._graph_neighborhood("../../evil")
    assert result == {"nodes": [], "edges": [], "center": None}


def test_graph_neighborhood_supports_mcp_nodes(monkeypatch) -> None:
    import networkx as nx
    import sys

    G = nx.Graph()
    G.add_node(
        "mcp-server:anthropic-python-sdk",
        label="anthropic-python-sdk",
        type="mcp-server",
        tags=["sdk"],
    )
    fake = type("M", (), {"load_graph": staticmethod(lambda _path=None: G)})
    monkeypatch.setitem(sys.modules, "ctx.core.graph.resolve_graph", fake)
    monkeypatch.setitem(sys.modules, "resolve_graph", fake)

    result = cm._graph_neighborhood("anthropic-python-sdk")
    assert result["center"] == "mcp-server:anthropic-python-sdk"
    assert result["nodes"][0]["data"]["type"] == "mcp-server"


def test_graph_helpers_reuse_graph_loaded_from_same_file(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx
    import sys

    graph_file = fake_claude / "skill-wiki" / "graphify-out" / "graph.json"
    graph_file.parent.mkdir(parents=True)
    graph_file.write_text("{}", encoding="utf-8")
    G = nx.Graph()
    G.add_edge("skill:python-patterns", "skill:fastapi-pro", weight=2)
    G.nodes["skill:python-patterns"]["label"] = "python-patterns"

    calls: list[Path | None] = []

    def _load_graph(path: Path | None = None):
        calls.append(path)
        return G

    fake = type("M", (), {"load_graph": staticmethod(_load_graph)})
    monkeypatch.setitem(sys.modules, "ctx.core.graph.resolve_graph", fake)
    monkeypatch.setattr(cm, "_GRAPH_CACHE_KEY", None)
    monkeypatch.setattr(cm, "_GRAPH_CACHE_VALUE", None)

    assert cm._graph_stats()["nodes"] == 2
    assert cm._top_degree_seeds(limit=1)[0]["slug"] == "python-patterns"
    assert cm._graph_neighborhood("python-patterns")["center"] == "skill:python-patterns"
    assert calls == [graph_file]


def test_graph_neighborhood_empty_when_graph_absent(monkeypatch) -> None:
    # Force load_graph to raise so the helper returns the empty shape
    # deterministically, independent of whether the user's graph is built.
    import ctx_monitor as cm_mod

    def _bad(*_a, **_k):
        raise RuntimeError("no graph")

    # resolve_graph may be imported lazily inside _graph_neighborhood — force
    # the import to yield a stub that raises.
    import sys
    fake = type("M", (), {"load_graph": _bad})
    # ctx_monitor lazy-imports 'from ctx.core.graph.resolve_graph import load_graph'
    # at call time; inject at the canonical dotted path so the lazy import
    # resolves to our stub. Also populate the legacy shim path belt-and-
    # braces in case a downstream path still routes through it.
    monkeypatch.setitem(sys.modules, "ctx.core.graph.resolve_graph", fake)
    monkeypatch.setitem(sys.modules, "resolve_graph", fake)
    result = cm_mod._graph_neighborhood("python-patterns")
    assert result == {"nodes": [], "edges": [], "center": None}


def test_render_home_shows_stat_grid_even_with_no_sessions(fake_claude: Path) -> None:
    """The previous home page was near-blank when the user had no sessions.
    The rc11 layout must show the six stat cards + grade pills + empty-state
    messages so the page never feels empty."""
    html_out = cm._render_home()
    # Six stat-card titles must all be present regardless of data volume.
    for label in ("Currently loaded", "Sidecars", "Wiki entities",
                  "Knowledge graph", "Audit events", "Sessions"):
        assert label in html_out
    # Links into each section.
    for href in ("/loaded", "/skills", "/graph", "/logs", "/events", "/sessions"):
        assert href in html_out
    # Grade pills always render (values may be zero).
    for grade in ("A", "B", "C", "D", "F"):
        assert f"grade-{grade}" in html_out
    # Empty-state copy kicks in when there are no sessions / audit entries.
    assert "No sessions recorded" in html_out or "Recent sessions" in html_out


def test_render_skills_emits_sidebar_filters(fake_claude: Path) -> None:
    _write_sidecar(fake_claude, "a", {"slug": "a", "grade": "A", "raw_score": 0.9,
                                       "subject_type": "skill"})
    _write_sidecar(fake_claude, "b", {"slug": "b", "grade": "F", "raw_score": 0.1,
                                       "subject_type": "agent",
                                       "hard_floor": "intake_fail"})
    html_out = cm._render_skills()
    # Sidebar must expose a text search + grade checkboxes + type checkboxes.
    assert "id='skill-search'" in html_out
    assert "class='grade-filter'" in html_out
    assert "class='type-filter'" in html_out
    # Cards, not a legacy table-row element.
    assert "class='skill-card'" in html_out
    # Per-card links to sidecar/wiki/graph drill-downs.
    assert ">sidecar</a>" in html_out
    assert ">wiki</a>" in html_out
    assert ">graph</a>" in html_out


def test_render_wiki_index_lists_entities(fake_claude: Path) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    agents_dir = fake_claude / "skill-wiki" / "entities" / "agents"
    skills_dir.mkdir(parents=True)
    agents_dir.mkdir(parents=True)
    (skills_dir / "python-patterns.md").write_text(
        "---\nname: python-patterns\ntype: skill\n"
        "description: Idiomatic Python patterns\ntags: [python, patterns]\n"
        "---\n# body\n",
        encoding="utf-8",
    )
    (agents_dir / "code-reviewer.md").write_text(
        "---\nname: code-reviewer\ntype: agent\n"
        "description: Reviews code for issues\ntags: [review, quality]\n"
        "---\n# body\n",
        encoding="utf-8",
    )
    html_out = cm._render_wiki_index()
    # Both entities render as cards with their slug.
    assert "python-patterns" in html_out
    assert "code-reviewer" in html_out
    # Descriptions surface on the card.
    assert "Idiomatic Python patterns" in html_out
    assert "Reviews code for issues" in html_out
    # Search + type filter must be present.
    assert "id='wiki-search'" in html_out
    assert "class='wiki-type-filter'" in html_out
    # Cards link to the typed per-entity wiki page so duplicate slugs can
    # disambiguate skill/agent/MCP/harness pages.
    assert "href='/wiki/python-patterns?type=skill'" in html_out
    assert "href='/wiki/code-reviewer?type=agent'" in html_out


def test_render_wiki_index_does_not_bleed_grade_across_duplicate_slugs(
    fake_claude: Path,
) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    harnesses_dir = fake_claude / "skill-wiki" / "entities" / "harnesses"
    skills_dir.mkdir(parents=True)
    harnesses_dir.mkdir(parents=True)
    (skills_dir / "langgraph.md").write_text(
        "---\nname: langgraph\ntype: skill\n---\n# body\n",
        encoding="utf-8",
    )
    (harnesses_dir / "langgraph.md").write_text(
        "---\nname: langgraph\ntype: harness\n---\n# body\n",
        encoding="utf-8",
    )
    _write_sidecar(fake_claude, "langgraph", {
        "slug": "langgraph",
        "subject_type": "skill",
        "grade": "D",
        "raw_score": 0.2,
    })

    html_out = cm._render_wiki_index()

    harness_start = html_out.index("href='/wiki/langgraph?type=harness'")
    harness_card = html_out[harness_start:html_out.index("</a>", harness_start)]
    assert "grade-D" not in harness_card
    assert "<span class='pill'>harness</span>" in harness_card


def test_render_wiki_index_empty_when_no_entities(fake_claude: Path) -> None:
    html_out = cm._render_wiki_index()
    # No entities → still renders the chrome + a helpful empty state.
    assert "<h1>Wiki</h1>" in html_out
    assert "No wiki entities found" in html_out


def test_render_wiki_index_rejects_unsafe_filenames(fake_claude: Path) -> None:
    """A file on disk with a slug that doesn't pass the allowlist must
    not appear in the index, even if glob returns it."""
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skills_dir.mkdir(parents=True)
    # Valid entry alongside an uppercase-first filename (fails ^[a-z0-9]).
    (skills_dir / "python-patterns.md").write_text(
        "---\nname: python-patterns\n---\n", encoding="utf-8",
    )
    (skills_dir / "Bad-Start.md").write_text(
        "---\nname: Bad-Start\n---\n", encoding="utf-8",
    )
    entries = cm._wiki_index_entries()
    slugs = {e["slug"] for e in entries}
    assert "python-patterns" in slugs
    assert "Bad-Start" not in slugs


def test_render_kpi_empty_state(fake_claude: Path) -> None:
    """With no sidecars, /kpi must render a helpful empty state, not 500."""
    html_out = cm._render_kpi()
    assert "<h1>KPIs</h1>" in html_out
    assert "No KPI data yet" in html_out
    assert "ctx-skill-quality recompute --all" in html_out
    assert "ctx-skill-quality score --all" not in html_out


def test_render_kpi_with_sidecars(fake_claude: Path) -> None:
    """With real sidecars + lifecycle files, /kpi must surface the four
    main tables: grade distribution, lifecycle tiers, categories,
    demotion candidates. Mirrors kpi_dashboard.render_markdown sections."""
    _write_sidecar(fake_claude, "alpha", {
        "slug": "alpha", "subject_type": "skill",
        "grade": "A", "raw_score": 0.92, "score": 0.92,
        "hard_floor": None, "computed_at": "2026-04-19T10:00:00+00:00",
    })
    _write_sidecar(fake_claude, "foxtrot", {
        "slug": "foxtrot", "subject_type": "skill",
        "grade": "F", "raw_score": 0.08, "score": 0.08,
        "hard_floor": "intake_fail",
        "computed_at": "2026-04-19T10:00:00+00:00",
    })
    html_out = cm._render_kpi()
    # The five section headings must all be present.
    assert "Grade distribution" in html_out
    assert "Lifecycle tiers" in html_out
    assert "Top demotion candidates" in html_out
    assert "By category" in html_out
    assert "Archived" in html_out
    # Slug must be linked into the demotion-candidates table.
    assert "foxtrot" in html_out
    # Cross-link to the JSON endpoint + /skills drill-down.
    assert "/api/kpi.json" in html_out
    assert "/skills" in html_out


def test_api_kpi_summary_shape(fake_claude: Path) -> None:
    """The JSON endpoint must return a DashboardSummary-shaped dict."""
    _write_sidecar(fake_claude, "alpha", {
        "slug": "alpha", "subject_type": "skill",
        "grade": "A", "raw_score": 0.9, "score": 0.9,
        "hard_floor": None, "computed_at": "2026-04-19T10:00:00+00:00",
    })
    summary = cm._kpi_summary()
    assert summary is not None
    d = summary.to_dict()
    for key in ("total", "grade_counts", "lifecycle_counts",
                "category_breakdown", "hard_floor_counts",
                "low_quality_candidates", "archived", "generated_at"):
        assert key in d, f"missing {key}"
    assert d["total"] == 1
    assert d["grade_counts"].get("A", 0) == 1


def test_layout_nav_includes_wiki_and_kpi() -> None:
    """Every rendered page must include the new Wiki + KPI tabs in the
    top nav — the user explicitly asked for them to be accessible."""
    out = cm._layout("test", "<p>body</p>")
    assert "href='/wiki'" in out
    assert "href='/kpi'" in out
    assert "href='/graph'" in out
    assert ">Wiki<" in out
    assert ">KPIs<" in out


def test_render_graph_landing_shows_seeds_when_available(monkeypatch) -> None:
    """With a non-empty graph, /graph (no slug) should surface popular
    seed slugs so the first-time visitor has something to click."""
    import networkx as nx
    G = nx.Graph()
    # High-degree hub: 'skill:python-patterns' with 5 neighbors.
    for peer in ("skill:fastapi-pro", "skill:async-patterns",
                 "agent:code-reviewer", "skill:pydantic", "skill:sqlalchemy"):
        G.add_edge("skill:python-patterns", peer, weight=2)
    G.nodes["skill:python-patterns"]["label"] = "python-patterns"
    fake = type("M", (), {"load_graph": staticmethod(lambda _path=None: G)})
    import sys
    # ctx_monitor lazy-imports 'from ctx.core.graph.resolve_graph import load_graph'
    # at call time; inject at the canonical dotted path so the lazy import
    # resolves to our stub. Also populate the legacy shim path belt-and-
    # braces in case a downstream path still routes through it.
    monkeypatch.setitem(sys.modules, "ctx.core.graph.resolve_graph", fake)
    monkeypatch.setitem(sys.modules, "resolve_graph", fake)

    html_out = cm._render_graph(None)
    # Landing page shows the seeds block.
    assert "Popular seed slugs" in html_out
    # Hub slug must appear as a clickable chip.
    assert "python-patterns" in html_out
    # Graph stats line shows node/edge counts.
    assert "nodes" in html_out


def test_render_graph_landing_hides_seeds_when_graph_absent(monkeypatch) -> None:
    import sys

    def _bad(*_a, **_k):
        raise RuntimeError("no graph")

    fake = type("M", (), {"load_graph": _bad})
    # ctx_monitor lazy-imports 'from ctx.core.graph.resolve_graph import load_graph'
    # at call time; inject at the canonical dotted path so the lazy import
    # resolves to our stub. Also populate the legacy shim path belt-and-
    # braces in case a downstream path still routes through it.
    monkeypatch.setitem(sys.modules, "ctx.core.graph.resolve_graph", fake)
    monkeypatch.setitem(sys.modules, "resolve_graph", fake)
    html_out = cm._render_graph(None)
    # No seeds section when graph isn't available.
    assert "Popular seed slugs" not in html_out
    # But the search box and cytoscape mount still render.
    assert "id='focus'" in html_out
    assert "id='cy'" in html_out


def test_cli_argparser_exposes_serve() -> None:
    # argparse should not raise; subcommand "serve" is required
    with pytest.raises(SystemExit):
        cm.main([])
    # Valid invocation parses (we don't actually start the server; parse_args
    # returns args but cm.main() would call serve() which blocks. So just
    # test the parser path.)
    __import__("argparse").ArgumentParser()
    # Minimal smoke: main with --help exits 0.
    with pytest.raises(SystemExit) as exc:
        cm.main(["serve", "--help"])
    assert exc.value.code == 0


def test_monitor_server_suppresses_aborted_connection_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = cm._make_monitor_server("127.0.0.1", 0)
    try:
        monkeypatch.setattr(
            cm.sys,
            "exc_info",
            lambda: (ConnectionAbortedError, ConnectionAbortedError(), None),
        )
        server.handle_error(object(), ("127.0.0.1", 12345))
        captured = capsys.readouterr()
        assert captured.err == ""
    finally:
        server.server_close()
