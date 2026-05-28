"""Tests for ctx_monitor — dashboard aggregation and HTML rendering."""

from __future__ import annotations

import http.client
import hashlib
import json
import os
import sqlite3
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path

import pytest

import ctx_monitor as cm
import ctx_init as ci
from ctx.core.wiki import wiki_queue


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch) -> Path:
    """Point ctx_monitor at a throwaway ~/.claude tree."""
    claude = tmp_path / ".claude"
    (claude / "skill-quality").mkdir(parents=True)
    monkeypatch.setattr(cm, "_claude_dir", lambda: claude)
    monkeypatch.setattr(cm, "_dashboard_graph_index_archives", lambda: [])
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


def _write_runtime_events(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _write_graph_manifest(claude: Path, export_id: str) -> None:
    graph_dir = claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True, exist_ok=True)
    (graph_dir / "graph-export-manifest.json").write_text(
        json.dumps({"version": 1, "export_id": export_id}),
        encoding="utf-8",
    )


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


def _get_json(port: int, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _get_raw(
    port: int,
    path: str,
    *,
    headers: dict[str, str],
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        for key, value in headers.items():
            conn.putheader(key, value)
        conn.endheaders()
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload
    finally:
        conn.close()


def _post_raw(
    port: int,
    path: str,
    *,
    headers: dict[str, str],
    body: bytes = b"",
) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("POST", path, skip_host=True)
        if "Host" not in headers:
            conn.putheader("Host", f"127.0.0.1:{port}")
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
    third = wiki_queue.enqueue_maintenance_job(
        wiki,
        kind=wiki_queue.CATALOG_REFRESH_JOB,
        payload={"catalog": "graph/skills-sh-catalog.json.gz"},
        source="test",
        now=12.0,
    )
    leased = wiki_queue.lease_next(db_path, worker_id="worker-a", now=12.0)
    assert leased is not None
    wiki_queue.mark_failed(db_path, leased.id, error="boom", retry=False, now=13.0)
    wiki_queue.cancel_job(db_path, third.id, reason="operator skipped", now=14.0)

    status = cm._queue_status()
    html_out = cm._render_status()

    assert status["available"] is True
    assert status["counts"] == {
        wiki_queue.STATUS_PENDING: 1,
        wiki_queue.STATUS_RUNNING: 0,
        wiki_queue.STATUS_SUCCEEDED: 0,
        wiki_queue.STATUS_FAILED: 1,
        wiki_queue.STATUS_CANCELLED: 1,
    }
    assert status["total"] == 3
    assert [job["id"] for job in status["recent_jobs"]] == [third.id, second.id, first.id]
    assert status["recent_jobs"][0]["status"] == wiki_queue.STATUS_CANCELLED
    assert "cancelled: 1" in html_out


def test_artifact_status_reads_promotion_metadata(
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    graph = graph_dir / "graph.json"
    graph.write_text('{"nodes":[],"edges":[]}', encoding="utf-8")
    repo_graph = tmp_path / "repo-graph"
    repo_graph.mkdir()
    (repo_graph / "wiki-graph.tar.gz").write_bytes(b"tar")
    runtime_catalog = (
        fake_claude / "skill-wiki" / "external-catalogs" / "skills-sh" / "catalog.json"
    )
    runtime_catalog.parent.mkdir(parents=True)
    runtime_catalog.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cm, "_repo_graph_dir", lambda: repo_graph)
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
    assert status["wiki_graph_tar"]["path"] == str(repo_graph / "wiki-graph.tar.gz")
    assert status["skills_sh_catalog"]["path"] == str(runtime_catalog)
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


def test_status_page_shows_queue_db_errors(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = wiki_queue.queue_db_path(fake_claude / "skill-wiki")
    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"exists")

    def fail_count_jobs_by_status(_db_path: Path) -> dict[str, int]:
        raise RuntimeError("cannot read queue")

    monkeypatch.setattr(
        cm.wiki_queue,
        "count_jobs_by_status",
        fail_count_jobs_by_status,
    )

    status = cm._queue_status()
    html_out = cm._render_status()

    assert status["available"] is False
    assert status["error"] == "cannot read queue"
    assert "Queue DB error" in html_out
    assert "cannot read queue" in html_out
    assert "Durable worker DB: error" in html_out


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


def test_runtime_lifecycle_summary_reads_validation_and_escalation_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "runtime" / "events.jsonl"
    monkeypatch.setattr(cm, "_runtime_lifecycle_path", lambda: events)
    _write_runtime_events(events, [
        {
            "action": "validation",
            "session_id": "s-1",
            "check_name": "pytest",
            "status": "passed",
            "created_at": "2026-05-08T01:00:00Z",
        },
        {
            "action": "validation",
            "session_id": "s-1",
            "check_name": "mypy",
            "status": "failed",
            "summary": "type gate failed",
            "created_at": "2026-05-08T01:05:00Z",
        },
        {
            "action": "escalation",
            "session_id": "s-1",
            "trigger": "validation-failed",
            "reason": "mypy failed after retry",
            "status": "open",
            "severity": "blocking",
            "created_at": "2026-05-08T01:06:00Z",
        },
        {
            "action": "escalation",
            "session_id": "s-2",
            "trigger": "user-review",
            "reason": "review completed",
            "status": "resolved",
            "severity": "info",
            "created_at": "2026-05-08T01:07:00Z",
        },
    ])

    summary = cm._runtime_lifecycle_summary()

    assert summary["validations_total"] == 2
    assert summary["validation_failures"] == 1
    assert summary["open_escalations_total"] == 1
    assert summary["latest_validation"]["check_name"] == "mypy"
    assert summary["open_escalations"][0]["trigger"] == "validation-failed"


def test_runtime_lifecycle_summary_uses_full_history_for_open_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "runtime" / "events.jsonl"
    monkeypatch.setattr(cm, "_runtime_lifecycle_path", lambda: events)
    records = [{
        "action": "escalation",
        "session_id": "s-1",
        "trigger": "validation-failed",
        "reason": "pytest failed",
        "status": "open",
        "severity": "blocking",
        "created_at": "2026-05-08T00:00:00Z",
    }]
    records.extend({
        "action": "validation",
        "session_id": "s-1",
        "check_name": f"check-{idx}",
        "status": "passed",
        "created_at": f"2026-05-08T01:{idx % 60:02d}:00Z",
    } for idx in range(201))
    _write_runtime_events(events, records)

    summary = cm._runtime_lifecycle_summary()

    assert summary["validations_total"] == 201
    assert summary["open_escalations_total"] == 1
    assert summary["open_escalations"][0]["trigger"] == "validation-failed"

    records.append({
        "action": "escalation",
        "session_id": "s-1",
        "trigger": "validation-failed",
        "reason": "pytest failed",
        "status": "resolved",
        "severity": "blocking",
        "created_at": "2026-05-08T02:00:00Z",
    })
    _write_runtime_events(events, records)

    summary = cm._runtime_lifecycle_summary()
    assert summary["open_escalations_total"] == 0
    assert summary["escalations_total"] == 2


def test_render_runtime_lifecycle_surfaces_checks_and_open_escalations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = tmp_path / "runtime" / "events.jsonl"
    monkeypatch.setattr(cm, "_runtime_lifecycle_path", lambda: events)
    _write_runtime_events(events, [
        {
            "action": "validation",
            "session_id": "s-1",
            "check_name": "mypy",
            "status": "failed",
            "summary": "<type gate failed>",
            "created_at": "2026-05-08T01:05:00Z",
        },
        {
            "action": "escalation",
            "session_id": "s-1",
            "trigger": "validation-failed",
            "reason": "<mypy failed>",
            "status": "open",
            "severity": "blocking",
            "created_at": "2026-05-08T01:06:00Z",
        },
    ])

    html = cm._render_runtime_lifecycle()

    assert "Runtime lifecycle" in html
    assert "mypy" in html
    assert "validation-failed" in html
    assert "&lt;type gate failed&gt;" in html
    assert "&lt;mypy failed&gt;" in html


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


def test_monitor_post_rejects_rebound_host_with_valid_token(
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
                "Host": "evil.example",
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


def test_monitor_get_rejects_rebound_host_in_loopback_mode(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, payload = _get_raw(
            port,
            "/api/status.json",
            headers={"Host": "evil.example"},
        )
        assert status == 403
        assert "monitor read" in payload["detail"]
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
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/loaded", timeout=5)
        assert excinfo.value.code == 403
        assert "monitor read token required" in excinfo.value.read().decode("utf-8")

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/loaded?token=browser-token",
            timeout=5,
        ) as response:
            loaded_html = response.read().decode("utf-8")
            csp = response.headers.get("Content-Security-Policy", "")
        assert "browser-token" not in loaded_html
        assert "Read-only mode" in loaded_html
        assert "script-src 'self' 'unsafe-inline'" in csp

        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/manifest.json",
                timeout=5,
            )
        assert excinfo.value.code == 403
        body = json.loads(excinfo.value.read().decode("utf-8"))
        assert "read token required" in body["detail"]

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


def test_render_events_shows_recent_audit_backlog(fake_claude: Path) -> None:
    _write_audit(fake_claude, [
        {"ts": "t1", "event": "skill.loaded", "subject": "python-patterns"},
        {"ts": "t2", "event": "agent.loaded", "subject": "repo-reviewer"},
    ])

    html_out = cm._render_events()

    assert "Showing last 2 audit events" in html_out
    assert "skill.loaded" in html_out
    assert "python-patterns" in html_out
    assert "agent.loaded" in html_out
    assert "repo-reviewer" in html_out
    assert "id='stream-status'" in html_out


def test_live_alias_renders_events_page(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_audit(fake_claude, [
        {"ts": "t1", "event": "skill.loaded", "subject": "python-patterns"},
    ])
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/live", timeout=5) as response:
            html_out = response.read().decode("utf-8")
        assert response.status == 200
        assert "Live events" in html_out
        assert "python-patterns" in html_out
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
    assert "class='card wiki-body'" in html_out


def test_render_wiki_entity_renders_markdown_and_wikilinks(fake_claude: Path) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "markdown-page.md").write_text(
        "---\n"
        "type: skill\n"
        "tags: [python, testing]\n"
        "---\n"
        "# Markdown Page\n\n"
        "Use `pytest` with [[entities/skills/find-skills]].\n\n"
        "Source: [Homepage](https://example.com/docs).\n\n"
        "Unsafe: [bad](javascript:alert(1)).\n\n"
        "Protocol relative: [offsite](//evil.example/x).\n\n"
        "- first item\n"
        "- [[entities/agents/reviewer|reviewer agent]]\n",
        encoding="utf-8",
    )

    html_out = cm._render_wiki_entity("markdown-page", entity_type="skill")

    assert "<h1>Markdown Page</h1>" not in html_out
    assert "<code>pytest</code>" in html_out
    assert "href='/wiki/find-skills?type=skill'" in html_out
    assert "href='/wiki/reviewer?type=agent'" in html_out
    assert "<a href='https://example.com/docs'>Homepage</a>" in html_out
    assert "href='javascript:alert(1)'" not in html_out
    assert "href='//evil.example/x'" not in html_out
    assert "<li>first item</li>" in html_out
    assert "<pre style=" not in html_out


def test_render_mcp_wiki_entity_has_tabs_subgraph_and_quality(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    mcp_dir = fake_claude / "skill-wiki" / "entities" / "mcp-servers" / "g"
    mcp_dir.mkdir(parents=True)
    (mcp_dir / "github.md").write_text(
        "---\n"
        "type: mcp-server\n"
        "description: Manage repositories and issues.\n"
        "---\n"
        "# GitHub\n\n"
        "Manage repositories, issues, and search code via GitHub API.\n",
        encoding="utf-8",
    )
    _write_mcp_sidecar(fake_claude, "github", {
        "slug": "github",
        "subject_type": "mcp-server",
        "grade": "B",
        "raw_score": 0.7106,
        "hard_floor": "missing-license",
        "weights": {"freshness": 0.25, "docs": 0.75},
        "signals": {
            "docs": {"score": 0.9, "evidence": {"has_install": True}},
            "freshness": {"score": 0.2, "evidence": {"last_commit_at": None}},
        },
    })
    _write_sidecar(fake_claude, "github-actions", {
        "slug": "github-actions",
        "subject_type": "skill",
        "grade": "A",
        "raw_score": 0.91,
    })
    _write_sidecar(fake_claude, "repo-reviewer", {
        "slug": "repo-reviewer",
        "subject_type": "agent",
        "grade": "C",
        "raw_score": 0.43,
        "hard_floor": "needs-usage",
    })
    graph = nx.Graph()
    graph.add_node("mcp-server:github", label="github", type="mcp-server", tags=["reference"])
    graph.add_node("skill:github-actions", label="github-actions", type="skill", tags=["github", "ci"])
    graph.add_node("agent:repo-reviewer", label="repo-reviewer", type="agent", tags=["github", "review"])
    graph.add_edge("mcp-server:github", "skill:github-actions", weight=0.91, shared_tags=["github", "ci"])
    graph.add_edge("mcp-server:github", "agent:repo-reviewer", weight=0.83, shared_tags=["github"])
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: graph)

    html_out = cm._render_wiki_entity("github", entity_type="mcp-server")

    assert "data-entity-tab='overview'" in html_out
    assert "data-entity-tab='subgraph'" in html_out
    assert "data-entity-tab='quality'" in html_out
    assert "<h2>Subgraph</h2>" in html_out
    assert "data-testid='entity-subgraph-graph'" in html_out
    assert "data-testid='entity-subgraph-3d'" in html_out
    assert "data-testid=\"entity-subgraph-node\"" in html_out
    assert "data-testid=\"entity-subgraph-edge\"" in html_out
    assert "id='entity-subgraph-zoom-in'" in html_out
    assert "id='entity-subgraph-zoom-out'" in html_out
    assert "drag to rotate" in html_out
    assert "wheel to zoom" in html_out
    assert "data-testid='entity-subgraph-node-detail'" in html_out
    assert "data-testid='entity-subgraph-edge-detail'" in html_out
    assert "Open interactive graph view" not in html_out
    assert "href='/wiki/github-actions?type=skill'" in html_out
    assert "href='/wiki/repo-reviewer?type=agent'" in html_out
    assert "grade-A" in html_out
    assert "grade-C" in html_out
    assert "needs-usage" in html_out
    assert "<h2>Quality</h2>" in html_out
    assert "freshness" in html_out
    assert "has_install" in html_out
    assert "missing-license" in html_out
    assert "<pre style=" not in html_out


@pytest.mark.parametrize(
    ("entity_type", "subdir", "slug"),
    [
        ("skill", "skills", "quality-skill"),
        ("agent", "agents", "quality-agent"),
        ("mcp-server", "mcp-servers/q", "quality-mcp"),
        ("harness", "harnesses", "quality-harness"),
    ],
)
def test_render_wiki_entity_moves_embedded_quality_block_to_quality_tab(
    fake_claude: Path,
    entity_type: str,
    subdir: str,
    slug: str,
) -> None:
    entity_dir = fake_claude / "skill-wiki" / "entities" / subdir
    entity_dir.mkdir(parents=True)
    (entity_dir / f"{slug}.md").write_text(
        "---\n"
        f"type: {entity_type}\n"
        "---\n"
        f"# {slug}\n\n"
        "Overview body stays here.\n\n"
        "<!-- quality:begin -->\n"
        "## Quality\n\n"
        "- **Grade:** C\n"
        "- **Score:** 0.45 (raw 0.45)\n"
        "- **Computed:** 2026-05-09T15:47:33+00:00\n\n"
        "| Signal | Score | Weight |\n"
        "| --- | --- | --- |\n"
        "| telemetry | 0.45 | 0.40 |\n"
        "<!-- quality:end -->\n\n"
        "Overview continues here.\n",
        encoding="utf-8",
    )

    html_out = cm._render_wiki_entity(slug, entity_type=entity_type)

    overview_start = html_out.index("data-entity-tab-panel='overview'")
    overview_end = html_out.index("data-entity-tab-panel='subgraph'")
    overview_html = html_out[overview_start:overview_end]
    quality_start = html_out.index("data-entity-tab-panel='quality'")
    quality_html = html_out[quality_start:]

    assert "data-entity-tab='overview'" in html_out
    assert "data-entity-tab='quality'" in html_out
    assert "Overview body stays here." in overview_html
    assert "Overview continues here." in overview_html
    assert "quality:begin" not in html_out
    assert "quality:end" not in html_out
    assert "**Grade:** C" not in overview_html
    assert "**Score:** 0.45" not in overview_html
    assert "Grade:" in quality_html
    assert "0.45 (raw 0.45)" in quality_html
    assert "No quality sidecar exists" not in quality_html


def test_render_wiki_entity_missing_slug(fake_claude: Path) -> None:
    out = cm._render_wiki_entity("nope-not-here")
    assert "No wiki page" in out


def test_render_wiki_entity_falls_back_to_runtime_graph_metadata(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm, "_MONITOR_MUTATIONS_ENABLED", True)
    monkeypatch.setattr(cm, "_MONITOR_TOKEN", "runtime-token")

    def fake_graph(slug: str, **_kwargs) -> dict:
        assert slug == "github"
        return {
            "center": "mcp-server:github",
            "nodes": [{
                "data": {
                    "id": "mcp-server:github",
                    "label": "GitHub",
                    "type": "mcp-server",
                    "tags": ["git", "issues", "pull-requests"],
                    "description": "Manage repositories, issues, and pull requests.",
                    "quality_score": 0.81,
                    "usage_score": 0.42,
                    "degree": 27,
                },
            }],
            "edges": [],
        }

    monkeypatch.setattr(cm, "_graph_neighborhood", fake_graph)

    html_out = cm._render_wiki_entity("github", entity_type="mcp-server")

    assert "Runtime graph entity" in html_out
    assert "Manage repositories, issues, and pull requests." in html_out
    assert "git" in html_out
    assert "0.810" in html_out
    assert "ctx-init --graph --graph-install-mode full" in html_out
    assert "data-entity-tab='overview'" in html_out
    assert "data-entity-tab='subgraph'" in html_out
    assert "data-entity-tab='quality'" in html_out
    assert "data-testid='runtime-entity-load'" in html_out
    assert "/api/load" in html_out
    assert "X-CTX-Monitor-Token" in html_out
    assert "payload.detail || payload.msg || response.status" in html_out


def test_render_runtime_harness_entity_shows_install_commands(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_graph(slug: str, **_kwargs) -> dict:
        assert slug == "mirage"
        return {
            "center": "harness:mirage",
            "nodes": [{
                "data": {
                    "id": "harness:mirage",
                    "label": "Mirage",
                    "type": "harness",
                    "tags": ["harness", "sandbox"],
                    "description": "Virtual filesystem harness for agent tools.",
                },
            }],
            "edges": [],
        }

    monkeypatch.setattr(cm, "_graph_neighborhood", fake_graph)

    html_out = cm._render_wiki_entity("mirage", entity_type="harness")

    assert "Install harness" in html_out
    assert "ctx-harness-install mirage --dry-run" in html_out
    assert "ctx-harness-install mirage" in html_out
    assert "data-testid='runtime-entity-load'" not in html_out


def test_render_graph_uses_builtin_3d_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cm,
        "_graph_stats",
        lambda: {"nodes": 0, "edges": 0, "available": False},
    )
    html_out = cm._render_graph("python-patterns")
    assert "id='cy'" in html_out
    assert "https://unpkg.com" not in html_out
    assert "data-testid=\"graph-renderer\"" in html_out
    assert "data-testid=\"graph-3d\"" in html_out
    assert "button id=\"graph-zoom-in\"" in html_out
    assert "button id=\"graph-zoom-out\"" in html_out
    assert "data-testid=\"graph-edge-detail\"" in html_out
    assert "Graph renderer unavailable" not in html_out
    assert "Enter a slug to render the graph" in html_out
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


def test_graph_neighborhood_supports_mcp_nodes(
    fake_claude: Path,
    monkeypatch,
) -> None:
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


def test_graph_neighborhood_resolves_partial_slug(
    fake_claude: Path,
    monkeypatch,
) -> None:
    import networkx as nx

    G = nx.Graph()
    G.add_node(
        "skill:brainstorming",
        label="brainstorming",
        type="skill",
        tags=["creative", "planning"],
    )
    G.add_node(
        "skill:multi-agent-brainstorming",
        label="multi-agent-brainstorming",
        type="skill",
        tags=["creative", "agents"],
    )
    G.add_edge(
        "skill:brainstorming",
        "skill:multi-agent-brainstorming",
        weight=0.89,
        shared_tags=["creative"],
    )
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    result = cm._graph_neighborhood("brainstorm", entity_type="skill")

    assert result["center"] == "skill:brainstorming"
    assert result["resolved"]["query"] == "brainstorm"
    assert result["resolved"]["slug"] == "brainstorming"
    assert "brainstorming" in result["suggestions"]


def test_graph_neighborhood_sizes_nodes_by_score_usage_and_popularity(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    G = nx.Graph()
    G.add_node(
        "skill:hub",
        label="hub",
        type="skill",
        tags=["python"],
        quality_score=0.95,
        usage_score=0.8,
    )
    G.add_node(
        "skill:leaf-low",
        label="leaf-low",
        type="skill",
        tags=["python"],
        quality_score=0.1,
        usage_score=0.0,
    )
    G.add_node(
        "skill:leaf-high",
        label="leaf-high",
        type="skill",
        tags=["python"],
        quality_score=0.85,
        usage_score=0.7,
    )
    G.add_edge("skill:hub", "skill:leaf-low", weight=0.2)
    G.add_edge("skill:hub", "skill:leaf-high", weight=0.9)
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    result = cm._graph_neighborhood("hub", entity_type="skill")
    sizes = {
        node["data"]["id"]: node["data"]["node_size"]
        for node in result["nodes"]
    }

    assert sizes["skill:hub"] > sizes["skill:leaf-high"] > sizes["skill:leaf-low"]
    assert 8 <= sizes["skill:leaf-low"] <= 24
    assert 8 <= sizes["skill:hub"] <= 24
    hub = next(node["data"] for node in result["nodes"] if node["data"]["id"] == "skill:hub")
    assert hub["size_signal"] > 0
    assert "quality 0.950" in hub["size_reason"]
    assert "usage 0.800" in hub["size_reason"]
    assert "popularity" in hub["size_reason"]


def test_graph_neighborhood_reuses_sidecar_index_for_slug_fallback(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    monkeypatch.setattr(cm, "_SIDECAR_INDEX_CACHE_KEY", None)
    monkeypatch.setattr(cm, "_SIDECAR_INDEX_CACHE_VALUE", None)
    sidecar_dir = fake_claude / "skill-quality"
    for i in range(12):
        (sidecar_dir / f"alias-{i}.json").write_text(
            json.dumps({
                "slug": f"node-{i}",
                "subject_type": "skill",
                "score": 0.5,
                "signals": {"telemetry": {"score": 0.1}},
            }),
            encoding="utf-8",
        )

    G = nx.Graph()
    G.add_node("skill:center", label="center", type="skill", tags=["x"])
    for i in range(6):
        G.add_node(f"skill:node-{i}", label=f"node-{i}", type="skill", tags=["x"])
        G.add_edge("skill:center", f"skill:node-{i}", weight=1.0)
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    calls = 0
    original = cm._read_sidecar_file

    def counted(path: Path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(cm, "_read_sidecar_file", counted)

    result = cm._graph_neighborhood("center", entity_type="skill")

    assert result["center"] == "skill:center"
    assert calls == 12


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


def test_graph_neighborhood_uses_dashboard_index_without_full_graph_load(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_graph_manifest(fake_claude, "test-export")
    index_path = fake_claude / "skill-wiki" / "graphify-out" / "dashboard-neighborhoods.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("max_degree", "10"),
                ("export_id", json.dumps("test-export")),
                ("nodes_count", "2"),
                ("edges_count", "1"),
            ],
        )
        conn.executemany(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            [
                ("skill:python-patterns", "python-patterns", "skill", '["python"]', "", 0.9, 0.1, 10),
                ("skill:fastapi-pro", "fastapi-pro", "skill", '["python","api"]', "", 0.8, 0.0, 4),
            ],
        )
        conn.executemany(
            "INSERT INTO slug_index VALUES(?,?,?)",
            [
                ("python-patterns", "skill", "skill:python-patterns"),
                ("fastapi-pro", "skill", "skill:fastapi-pro"),
            ],
        )
        payload = zlib.compress(json.dumps([
            {
                "target": "skill:fastapi-pro",
                "weight": 0.8,
                "shared_tags": ["python"],
                "reasons": ["semantic"],
            }
        ]).encode("utf-8"))
        conn.execute("INSERT INTO neighbors VALUES(?,?)", ("skill:python-patterns", payload))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(
        cm,
        "_load_dashboard_graph",
        lambda: (_ for _ in ()).throw(AssertionError("full graph loaded")),
    )

    result = cm._graph_neighborhood("python-patterns", entity_type="skill")

    assert result["center"] == "skill:python-patterns"
    assert [node["data"]["id"] for node in result["nodes"]] == [
        "skill:python-patterns",
        "skill:fastapi-pro",
    ]
    assert result["edges"][0]["data"]["shared_tags"] == ["python"]
    assert cm._graph_stats() == {"nodes": 2, "edges": 1, "available": True}
    assert cm._wiki_stats() == {
        "skills": 2,
        "agents": 0,
        "mcps": 0,
        "harnesses": 0,
        "total": 2,
        "split_known": True,
    }


def test_graph_neighborhood_uses_dashboard_index_when_overlay_is_already_indexed(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_graph_manifest(fake_claude, "test-export")
    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    index_path = graph_dir / "dashboard-neighborhoods.sqlite3"
    overlay_path = graph_dir / "entity-overlays.jsonl"
    graph_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("max_degree", "10"),
                ("top_k", "40"),
                ("export_id", json.dumps("test-export")),
                ("nodes_count", "2"),
                ("edges_count", "1"),
            ],
        )
        conn.executemany(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            [
                ("harness:mirage", "mirage", "harness", '["sandbox"]', "", 0.82, 0.0, 10),
                ("skill:codex-review", "codex-review", "skill", '["review"]', "", 0.7, 0.0, 5),
            ],
        )
        conn.executemany(
            "INSERT INTO slug_index VALUES(?,?,?)",
            [
                ("mirage", "harness", "harness:mirage"),
                ("codex-review", "skill", "skill:codex-review"),
            ],
        )
        conn.execute(
            "INSERT INTO neighbors VALUES(?,?)",
            (
                "harness:mirage",
                zlib.compress(json.dumps([
                    {
                        "target": "skill:codex-review",
                        "weight": 0.18,
                        "shared_tags": ["sandbox"],
                    },
                ]).encode("utf-8")),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    overlay_path.write_text(
        json.dumps({
            "overlay_id": "mirage-overlay",
            "nodes": [{"id": "harness:mirage", "type": "harness"}],
            "edges": [{"source": "harness:mirage", "target": "skill:codex-review"}],
        })
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cm, "_dashboard_overlay_matches_known_release", lambda overlay: True)
    monkeypatch.setattr(
        cm,
        "_load_dashboard_graph",
        lambda: (_ for _ in ()).throw(AssertionError("full graph loaded")),
    )

    result = cm._graph_neighborhood("mirage", entity_type="harness")

    assert result["center"] == "harness:mirage"
    assert [node["data"]["id"] for node in result["nodes"]] == [
        "harness:mirage",
        "skill:codex-review",
    ]


def test_dashboard_overlay_release_hash_normalizes_crlf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    overlay = tmp_path / "entity-overlays.jsonl"
    overlay.write_bytes(b'{"overlay_id":"release"}\r\n')
    expected = hashlib.sha256(b'{"overlay_id":"release"}\n').hexdigest()
    monkeypatch.setattr(ci, "_GRAPH_ENTITY_OVERLAY_SHA256", expected)

    assert cm._dashboard_overlay_matches_known_release(overlay)


def test_graph_neighborhood_bypasses_index_for_local_overlay_even_when_node_exists(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    _write_graph_manifest(fake_claude, "test-export")
    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    index_path = graph_dir / "dashboard-neighborhoods.sqlite3"
    overlay_path = graph_dir / "entity-overlays.jsonl"
    graph_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("max_degree", "10"),
                ("top_k", "40"),
                ("export_id", json.dumps("test-export")),
                ("nodes_count", "1"),
                ("edges_count", "0"),
            ],
        )
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            ("skill:existing", "existing", "skill", "[]", "", 0.7, 0.0, 0),
        )
        conn.execute(
            "INSERT INTO slug_index VALUES(?,?,?)",
            ("existing", "skill", "skill:existing"),
        )
        conn.execute(
            "INSERT INTO neighbors VALUES(?,?)",
            ("skill:existing", zlib.compress(b"[]")),
        )
        conn.commit()
    finally:
        conn.close()
    overlay_path.write_text(
        json.dumps({
            "kind": "ann_attach",
            "nodes": [{"id": "skill:existing", "type": "skill"}],
            "edges": [{"source": "skill:existing", "target": "skill:new-runtime-edge"}],
        })
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cm,
        "_graph_neighborhood_from_index",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("index used")),
    )
    G = nx.Graph()
    G.add_node("skill:existing", label="existing", type="skill", tags=[])
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    result = cm._graph_neighborhood("existing", entity_type="skill")

    assert result["center"] == "skill:existing"


def test_graph_index_honors_requested_type_on_exact_slug(
    fake_claude: Path,
) -> None:
    _write_graph_manifest(fake_claude, "test-export")
    index_path = fake_claude / "skill-wiki" / "graphify-out" / "dashboard-neighborhoods.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [("max_degree", "1"), ("top_k", "40"), ("export_id", json.dumps("test-export"))],
        )
        conn.executemany(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            [
                ("skill:github", "github", "skill", "[]", "", None, None, 0),
                ("mcp-server:github", "github", "mcp-server", "[]", "", None, None, 0),
            ],
        )
        conn.executemany(
            "INSERT INTO slug_index VALUES(?,?,?)",
            [
                ("github", "skill", "skill:github"),
                ("github", "mcp-server", "mcp-server:github"),
            ],
        )
        conn.executemany(
            "INSERT INTO neighbors VALUES(?,?)",
            [
                ("skill:github", zlib.compress(b"[]")),
                ("mcp-server:github", zlib.compress(b"[]")),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    result = cm._graph_neighborhood("github", entity_type="skill")

    assert result["center"] == "skill:github"


def test_graph_index_matches_fuzzy_slug_resolution(
    fake_claude: Path,
) -> None:
    _write_graph_manifest(fake_claude, "test-export")
    index_path = fake_claude / "skill-wiki" / "graphify-out" / "dashboard-neighborhoods.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [("max_degree", "1"), ("top_k", "40"), ("export_id", json.dumps("test-export"))],
        )
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            ("skill:github-actions", "GitHub Actions", "skill", '["ci"]', "", None, None, 0),
        )
        conn.execute(
            "INSERT INTO slug_index VALUES(?,?,?)",
            ("github-actions", "skill", "skill:github-actions"),
        )
        conn.execute("INSERT INTO neighbors VALUES(?,?)", ("skill:github-actions", zlib.compress(b"[]")))
        conn.commit()
    finally:
        conn.close()

    result = cm._graph_neighborhood("git hub", entity_type="skill")

    assert result["center"] == "skill:github-actions"
    assert result["resolved"]["slug"] == "github-actions"


def test_entity_subgraph_script_json_escapes_script_end_tag() -> None:
    graph_html = cm._render_entity_subgraph_svg(
        center="skill:evil",
        node_by_id={
            "skill:evil": {
                "label": "</script><script>alert(1)</script>",
                "type": "skill",
            },
        },
        edges=[],
        sidecar_by_id={},
    )

    assert "</script><script>alert(1)</script>" not in graph_html
    assert "<\\/script>" in graph_html


def test_graph_page_initial_query_escapes_script_end_tag() -> None:
    html_out = cm._render_graph("</script><script>alert(1)</script>")

    assert "const initial = \"<\\/script>" in html_out
    assert "const initial = \"</script>" not in html_out


def test_graph_neighborhood_extracts_missing_dashboard_index_from_archive(
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = tmp_path / "dashboard-neighborhoods.sqlite3"
    conn = sqlite3.connect(seed)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [
                ("max_degree", "10"),
                ("export_id", json.dumps("archive-export")),
                ("nodes_count", "1"),
                ("edges_count", "0"),
                ("top_k", "40"),
            ],
        )
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            ("skill:python-patterns", "python-patterns", "skill", '["python"]', "", 0.9, 0.1, 10),
        )
        conn.execute(
            "INSERT INTO slug_index VALUES(?,?,?)",
            ("python-patterns", "skill", "skill:python-patterns"),
        )
        conn.execute(
            "INSERT INTO neighbors VALUES(?,?)",
            ("skill:python-patterns", zlib.compress(b"[]")),
        )
        conn.commit()
    finally:
        conn.close()

    manifest = fake_claude / "skill-wiki" / "graphify-out" / "graph-export-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"version": 1, "export_id": "archive-export"}),
        encoding="utf-8",
    )

    archive = tmp_path / "wiki-graph-runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(seed, arcname="./graphify-out/dashboard-neighborhoods.sqlite3")

    monkeypatch.setattr(cm, "_dashboard_graph_index_archives", lambda: [archive])
    monkeypatch.setattr(
        cm,
        "_load_dashboard_graph",
        lambda: (_ for _ in ()).throw(AssertionError("full graph loaded")),
    )

    result = cm._graph_neighborhood("python-patterns", entity_type="skill")

    assert result["center"] == "skill:python-patterns"
    assert (fake_claude / "skill-wiki" / "graphify-out" / "dashboard-neighborhoods.sqlite3").is_file()


def test_dashboard_index_extraction_skips_archive_export_mismatch(
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph-export-manifest.json").write_text(
        json.dumps({"version": 1, "export_id": "local-export"}),
        encoding="utf-8",
    )
    manifest = tmp_path / "graph-export-manifest.json"
    manifest.write_text(
        json.dumps({"version": 1, "export_id": "archive-export"}),
        encoding="utf-8",
    )
    seed = tmp_path / "dashboard-neighborhoods.sqlite3"
    seed.write_bytes(b"should-not-be-extracted")
    archive = tmp_path / "wiki-graph-runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(manifest, arcname="./graphify-out/graph-export-manifest.json")
        tar.add(seed, arcname="./graphify-out/dashboard-neighborhoods.sqlite3")

    monkeypatch.setattr(cm, "_dashboard_graph_index_archives", lambda: [archive])
    monkeypatch.setattr(
        cm,
        "_dashboard_index_matches_manifest",
        lambda path: (_ for _ in ()).throw(
            AssertionError(f"should not validate extracted index: {path}"),
        ),
    )

    assert cm._ensure_dashboard_graph_index() is None
    assert not (graph_dir / "dashboard-neighborhoods.sqlite3").exists()


def test_dashboard_index_extraction_skips_installed_graph_report(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph-export-manifest.json").write_text(
        json.dumps({"version": 1, "export_id": "local-export"}),
        encoding="utf-8",
    )
    (graph_dir / "graph-report.md").write_text(
        "> Nodes: 12 | Edges: 34 | Communities: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cm,
        "_dashboard_graph_index_archives",
        lambda: (_ for _ in ()).throw(AssertionError("archive scan should be skipped")),
    )

    assert cm._ensure_dashboard_graph_index() is None


def test_graph_neighborhood_bypasses_archive_index_when_runtime_overlays_exist(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    overlay = fake_claude / "skill-wiki" / "graphify-out" / "entity-overlays.jsonl"
    overlay.parent.mkdir(parents=True)
    overlay.write_text('{"source":"test"}\n', encoding="utf-8")
    monkeypatch.setattr(
        cm,
        "_dashboard_graph_index_archives",
        lambda: (_ for _ in ()).throw(AssertionError("archive index should be bypassed")),
    )
    G = nx.Graph()
    G.add_node("skill:local-overlay", label="local-overlay", type="skill", tags=["local"])
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    result = cm._graph_neighborhood("local-overlay", entity_type="skill")

    assert result["center"] == "skill:local-overlay"


def test_graph_neighborhood_rejects_stale_dashboard_index(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    (graph_dir / "graph-export-manifest.json").write_text(
        json.dumps({"version": 1, "export_id": "new-export"}),
        encoding="utf-8",
    )
    index_path = graph_dir / "dashboard-neighborhoods.sqlite3"
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany("INSERT INTO meta VALUES(?,?)", [("export_id", json.dumps("old-export"))])
        conn.commit()
    finally:
        conn.close()

    G = nx.Graph()
    G.add_node("skill:fallback", label="fallback", type="skill", tags=[])
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    result = cm._graph_neighborhood("fallback", entity_type="skill")

    assert result["center"] == "skill:fallback"
    assert not index_path.exists()


def test_graph_neighborhood_rejects_orphan_dashboard_index_without_manifest(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import networkx as nx

    graph_dir = fake_claude / "skill-wiki" / "graphify-out"
    graph_dir.mkdir(parents=True)
    index_path = graph_dir / "dashboard-neighborhoods.sqlite3"
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT INTO meta VALUES(?,?)", [("export_id", json.dumps("old-export"))])
        conn.commit()
    finally:
        conn.close()

    G = nx.Graph()
    G.add_node("skill:fallback", label="fallback", type="skill", tags=[])
    monkeypatch.setattr(cm, "_load_dashboard_graph", lambda: G)

    result = cm._graph_neighborhood("fallback", entity_type="skill")

    assert result["center"] == "skill:fallback"
    assert not index_path.exists()


def test_graph_index_node_size_uses_live_sidecar_when_scores_missing(
    fake_claude: Path,
) -> None:
    _write_graph_manifest(fake_claude, "test-export")
    _write_sidecar(fake_claude, "python-patterns", {
        "slug": "python-patterns",
        "subject_type": "skill",
        "grade": "A",
        "score": 1.0,
        "signals": {"telemetry": {"score": 1.0}},
    })
    index_path = fake_claude / "skill-wiki" / "graphify-out" / "dashboard-neighborhoods.sqlite3"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE nodes(id TEXT PRIMARY KEY,label TEXT,type TEXT,tags TEXT,"
            "description TEXT,quality_score REAL,usage_score REAL,degree INTEGER)"
        )
        conn.execute(
            "CREATE TABLE slug_index(slug TEXT,type TEXT,node_id TEXT,"
            "PRIMARY KEY(slug,type,node_id))"
        )
        conn.execute("CREATE TABLE neighbors(source TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        conn.executemany(
            "INSERT INTO meta VALUES(?,?)",
            [("max_degree", "1"), ("top_k", "40"), ("export_id", json.dumps("test-export"))],
        )
        conn.execute(
            "INSERT INTO nodes VALUES(?,?,?,?,?,?,?,?)",
            ("skill:python-patterns", "python-patterns", "skill", "[]", "", None, None, 0),
        )
        conn.execute(
            "INSERT INTO slug_index VALUES(?,?,?)",
            ("python-patterns", "skill", "skill:python-patterns"),
        )
        conn.execute("INSERT INTO neighbors VALUES(?,?)", ("skill:python-patterns", zlib.compress(b"[]")))
        conn.commit()
    finally:
        conn.close()

    result = cm._graph_neighborhood("python-patterns", entity_type="skill")
    node = result["nodes"][0]["data"]

    assert node["size_signal"] > 0.7
    assert "quality 1.000" in node["size_reason"]
    assert "usage 1.000" in node["size_reason"]


def test_graph_neighborhood_empty_when_graph_absent(
    fake_claude: Path,
    monkeypatch,
) -> None:
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


def test_render_home_defers_sidecar_grade_scan(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_scan() -> dict[str, int]:
        raise AssertionError("home page should not synchronously scan sidecars")

    monkeypatch.setattr(cm, "_grade_distribution", fail_scan)

    html_out = cm._render_home()

    assert "/api/grades.json" in html_out
    assert "home-sidecar-count" in html_out
    assert "data-home-grade='A'" in html_out


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


def test_render_wiki_index_hides_legacy_skill_source_prefix(fake_claude: Path) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "skills-sh-owner-repo-brainstorming.md").write_text(
        "---\nname: skills-sh-owner-repo-brainstorming\ntype: skill\n"
        "description: Brainstorm with constraints\ntags: [brainstorming]\n"
        "---\n# body\n",
        encoding="utf-8",
    )

    html_out = cm._render_wiki_index()

    assert "<code style='font-size:0.84rem;'>owner-repo-brainstorming</code>" in html_out
    assert "href='/wiki/skills-sh-owner-repo-brainstorming?type=skill'" in html_out


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


def test_render_manage_includes_crud_and_upload_wizard(fake_claude: Path) -> None:
    html_out = cm._render_manage(mutations_enabled=True)

    assert "<h1>Manage catalog</h1>" in html_out
    assert "id='manage-search'" in html_out
    assert "id='entity-editor-form'" in html_out
    assert "data-testid='entity-delete-button'" in html_out
    assert "Add or update entity" in html_out


def test_entity_search_and_detail_apis_support_edit_flow(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skills_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "python-patterns.md").write_text(
        "---\n"
        "title: Python Patterns\n"
        "type: skill\n"
        "description: Idiomatic Python patterns\n"
        "tags: [python, patterns]\n"
        "---\n"
        "# Python Patterns\n\nUse dataclasses and context managers.\n",
        encoding="utf-8",
    )
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, payload = _get_json(
            port,
            "/api/entities/search.json?q=patterns&type=skill",
        )
        assert status == 200
        assert payload["results"][0]["slug"] == "python-patterns"
        assert payload["results"][0]["type"] == "skill"

        status, detail = _get_json(
            port,
            "/api/entity/python-patterns.json?type=skill",
        )
        assert status == 200
        assert detail["slug"] == "python-patterns"
        assert detail["frontmatter"]["description"] == "Idiomatic Python patterns"
        assert "Use dataclasses" in detail["body"]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_entity_upsert_api_writes_wiki_page_and_queues_graph_refresh(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, payload = _post_json(
            port,
            "/api/entity/upsert",
            {
                "slug": "custom-reviewer",
                "entity_type": "agent",
                "title": "Custom Reviewer",
                "description": "Reviews Python changes with local policy.",
                "tags": "python, review, policy",
                "source_url": "https://example.com/custom-reviewer",
                "body": "# Custom Reviewer\n\nUse this agent before merging Python changes.\n",
            },
            token="test-token",
        )
        assert status == 200
        assert payload["ok"] is True
        assert payload["detail"].startswith("saved agent:custom-reviewer")

        entity_path = (
            fake_claude
            / "skill-wiki"
            / "entities"
            / "agents"
            / "custom-reviewer.md"
        )
        text = entity_path.read_text(encoding="utf-8")
        assert "title: Custom Reviewer" in text
        assert "type: agent" in text
        assert "tags: [python, review, policy]" in text
        assert "Use this agent before merging Python changes." in text

        jobs = wiki_queue.list_recent_jobs(
            wiki_queue.queue_db_path(fake_claude / "skill-wiki"),
            limit=10,
        )
        assert [job.kind for job in jobs[:2]] == [
            wiki_queue.GRAPH_EXPORT_JOB,
            wiki_queue.ENTITY_UPSERT_JOB,
        ]
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_entity_upsert_api_requires_confirmation_for_existing_page(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity_path = (
        fake_claude / "skill-wiki" / "entities" / "agents" / "custom-reviewer.md"
    )
    entity_path.parent.mkdir(parents=True)
    entity_path.write_text(
        "---\ntitle: Custom Reviewer\ntype: agent\ntags: [python]\n"
        "---\n# Custom Reviewer\n\nOriginal body.\n",
        encoding="utf-8",
    )
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, payload = _post_json(
            port,
            "/api/entity/upsert",
            {
                "slug": "custom-reviewer",
                "entity_type": "agent",
                "title": "Custom Reviewer",
                "body": "# Custom Reviewer\n\nReplacement body.\n",
            },
            token="test-token",
        )
        assert status == 400
        assert payload["ok"] is False
        assert "confirm_update=true" in payload["detail"]
        assert "Original body." in entity_path.read_text(encoding="utf-8")

        status, payload = _post_json(
            port,
            "/api/entity/upsert",
            {
                "slug": "custom-reviewer",
                "entity_type": "agent",
                "title": "Custom Reviewer",
                "body": "# Custom Reviewer\n\nReplacement body.\n",
                "confirm_update": "true",
            },
            token="test-token",
        )
        assert status == 200
        assert payload["ok"] is True
        assert "Replacement body." in entity_path.read_text(encoding="utf-8")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_entity_delete_api_removes_wiki_page_and_queues_graph_refresh(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_dir = fake_claude / "skill-wiki" / "entities" / "harnesses"
    harness_dir.mkdir(parents=True)
    entity_path = harness_dir / "local-harness.md"
    entity_path.write_text(
        "---\ntitle: Local Harness\ntype: harness\ntags: [local, llm]\n"
        "---\n# Local Harness\n",
        encoding="utf-8",
    )
    server, thread, port = _serve_monitor(monkeypatch)
    try:
        status, payload = _post_json(
            port,
            "/api/entity/delete",
            {"slug": "local-harness", "entity_type": "harness"},
            token="test-token",
        )
        assert status == 200
        assert payload == {
            "ok": True,
            "detail": "deleted harness:local-harness and queued graph refresh",
        }
        assert not entity_path.exists()

        jobs = wiki_queue.list_recent_jobs(
            wiki_queue.queue_db_path(fake_claude / "skill-wiki"),
            limit=10,
        )
        assert [job.kind for job in jobs] == [wiki_queue.ENTITY_UPSERT_JOB]
        assert jobs[0].payload["action"] == "delete"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_entity_delete_unloads_live_entity_before_removing_page(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = fake_claude / "skill-wiki" / "entities" / "skills"
    skill_dir.mkdir(parents=True)
    entity_path = skill_dir / "python-patterns.md"
    entity_path.write_text(
        "---\ntitle: Python Patterns\ntype: skill\n---\n# Python Patterns\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cm,
        "_read_manifest",
        lambda: {"load": [{"skill": "python-patterns", "entity_type": "skill"}]},
    )

    def fake_unload(slug: str, entity_type: str = "skill") -> tuple[bool, str]:
        calls.append((slug, entity_type))
        return True, "unloaded"

    monkeypatch.setattr(cm, "_perform_unload", fake_unload)

    ok, detail = cm._delete_wiki_entity("python-patterns", "skill")

    assert ok is True
    assert "deleted skill:python-patterns" in detail
    assert calls == [("python-patterns", "skill")]
    assert not entity_path.exists()


def test_entity_delete_keeps_page_when_live_unload_fails(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_dir = fake_claude / "skill-wiki" / "entities" / "harnesses"
    harness_dir.mkdir(parents=True)
    entity_path = harness_dir / "local-harness.md"
    entity_path.write_text(
        "---\ntitle: Local Harness\ntype: harness\n---\n# Local Harness\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cm,
        "_read_manifest",
        lambda: {"load": [{"skill": "local-harness", "entity_type": "harness"}]},
    )
    monkeypatch.setattr(
        cm,
        "_perform_unload",
        lambda slug, entity_type="skill": (False, "use ctx-harness-install"),
    )

    ok, detail = cm._delete_wiki_entity("local-harness", "harness")

    assert ok is False
    assert "is loaded" in detail
    assert entity_path.exists()


def test_sidecar_cache_invalidates_on_file_rewrite(fake_claude: Path) -> None:
    cm._SIDECAR_INDEX_CACHE_KEY = None
    cm._SIDECAR_INDEX_CACHE_VALUE = None
    path = fake_claude / "skill-quality" / "alpha.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"slug": "alpha", "subject_type": "skill", "grade": "A"}),
        encoding="utf-8",
    )
    assert cm._all_sidecars()[0]["grade"] == "A"

    path.write_text(
        json.dumps({"slug": "alpha", "subject_type": "skill", "grade": "F"}),
        encoding="utf-8",
    )
    os.utime(path, (time.time() + 2.0, time.time() + 2.0))

    assert cm._all_sidecars()[0]["grade"] == "F"


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
    _write_sidecar(fake_claude, "reviewer", {
        "slug": "reviewer", "subject_type": "agent",
        "grade": "F", "raw_score": 0.06, "score": 0.06,
        "hard_floor": None,
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
    assert "/skill/reviewer?type=agent" in html_out
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
    assert "href='/harness'" in out
    assert "href='/docs'" in out
    assert "href='/kpi'" in out
    assert "href='/graph'" in out
    assert "href='/config'" in out
    assert ">Wiki<" in out
    assert ">Harness Setup<" in out
    assert ">Docs<" in out
    assert ">KPIs<" in out
    assert ">Config<" in out
    assert "--surface" in out
    assert "--accent" in out


def test_render_docs_lists_repo_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "harness").mkdir()
    (tmp_path / "graph").mkdir()
    (tmp_path / "mkdocs.yml").write_text(
        "\n".join([
            "nav:",
            "  - Home: index.md",
            "  - Dashboard: dashboard.md",
            "  - Harness:",
            "      - Attach to hosts: harness/attaching-to-hosts.md",
            "extra:",
            "  custom: !!python/name:material.extensions.emoji.twemoji",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text(
        "# ctx\n\nMain repo docs.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "index.md").write_text(
        "\n".join([
            "# Home",
            "",
            "Repo docs home.",
            "",
            "!!! tip \"Install\"",
            "",
            "    ctx-init --graph",
            "",
            "## Explore the docs",
            "",
            "Jump to the [dashboard docs](dashboard.md).",
            "",
            "### Deep section",
            "",
            "Nested docs body.",
            "",
            "<div class=\"grid cards\" markdown>",
            "",
            "-   **Knowledge graph**",
            "",
            "    ---",
            "",
            "    Graph docs.",
            "",
            "    [:octicons-arrow-right-24: Knowledge graph](knowledge-graph.md)",
            "",
            "</div>",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "docs" / "dashboard.md").write_text(
        "# Dashboard\n\n- **Monitor** skills, agents, MCPs, harnesses, and graph state.\n\n## Runtime view\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "knowledge-graph.md").write_text(
        "# Knowledge graph\n\n## Graph artifact\n\nGraph internals.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "harness" / "attaching-to-hosts.md").write_text(
        "# Attach to hosts\n\nConnect ctx to a non-Claude harness.\n",
        encoding="utf-8",
    )
    (tmp_path / "graph" / "README.md").write_text(
        "# Graph Artifacts\n\nCompressed wiki and knowledge graph artifacts.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cm, "_docs_roots", lambda: [tmp_path])

    html_out = cm._render_docs()

    assert "<h1>Docs</h1>" in html_out
    assert "class='docs-shell'" in html_out
    assert "class='docs-hero'" in html_out
    assert "Repo documentation" in html_out
    assert "sections</span>" in html_out
    assert "pages</span>" in html_out
    assert "class='docs-tabs'" in html_out
    assert "data-doc-tab='home'" in html_out
    assert "data-doc-tab='dashboard'" in html_out
    assert "data-doc-tab='harness'" in html_out
    assert "data-doc-tab='repo'" in html_out
    assert "README.md" in html_out
    assert "docs/dashboard.md" in html_out
    assert "docs/harness/attaching-to-hosts.md" in html_out
    assert "graph/README.md" in html_out
    assert "<strong>Monitor</strong> skills, agents, MCPs, harnesses, and graph state." in html_out
    assert "Connect ctx to a non-Claude harness." in html_out
    assert "class=\"admonition tip\"" in html_out
    assert "docs-heading-link docs-heading-level-2" in html_out
    assert "docs-heading-link docs-heading-level-3" in html_out
    assert "href='#doc-home-docs-index-md-explore-the-docs'" in html_out
    assert "href='#doc-home-docs-index-md-deep-section'" in html_out
    assert 'href="#doc-dashboard-docs-dashboard-md"' in html_out
    assert 'href="#doc-other-docs-knowledge-graph-md"' in html_out
    assert 'data-doc-tab="other"' in html_out
    assert "id='docs-search-results'" in html_out
    assert "jumpToDocTarget" in html_out
    assert '<div class="grid cards">' in html_out
    assert "<strong>Knowledge graph</strong>" in html_out
    assert "-&gt; Knowledge graph" in html_out
    assert ":octicons-arrow-right-24:" not in html_out
    assert "!!! tip" not in html_out
    assert "**Monitor**" not in html_out
    assert "id='docs-search'" in html_out
    assert "https://stevesolun.github.io/ctx/" in html_out
    assert "doc-card" not in html_out


def test_render_docs_falls_back_to_public_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm, "_docs_roots", lambda: [])

    html_out = cm._render_docs()

    assert "No local docs found." in html_out
    assert "https://stevesolun.github.io/ctx/" in html_out


def test_layout_nav_tabs_are_draggable_and_persist_order() -> None:
    out = cm._layout("test", "<p>body</p>")

    assert "name='viewport'" in out
    assert "id='dashboard-nav'" in out
    assert "data-nav-storage-key='ctx-monitor-nav-order'" in out
    assert "draggable='true'" in out
    assert "data-nav-key='graph'" in out
    assert "localStorage.setItem(storageKey" in out
    assert "dragstart" in out
    assert "function insertionTarget" in out
    assert "nav.addEventListener('dragover'" in out
    assert "nav.insertBefore(dragged, target" in out
    assert "drop" in out
    assert "id='nav-reset'" in out


def test_render_config_page_shows_required_defaults_and_examples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cm, "_claude_dir", lambda: tmp_path / ".claude")

    html_out = cm._render_config()

    assert "<h1>Config</h1>" in html_out
    assert "skill_transformer.line_threshold" in html_out
    assert "resolver.recommendation_top_k" in html_out
    assert "graph.semantic.min_cosine" in html_out
    assert "knowledge.mode" in html_out
    assert "Required" in html_out
    assert "Default: <code>180</code>" in html_out
    assert "Example:" in html_out
    assert "id='config-form'" in html_out


def test_render_harness_wizard_guides_model_choice_and_real_commands(
    fake_claude: Path,
) -> None:
    harness_dir = fake_claude / "skill-wiki" / "entities" / "harnesses"
    harness_dir.mkdir(parents=True)
    (harness_dir / "langgraph.md").write_text(
        "\n".join([
            "---",
            "title: LangGraph harness",
            "type: harness",
            "description: Durable Python agent workflows with tool routing.",
            "tags: [python, api, local, verification]",
            "repo_url: https://github.com/langchain-ai/langgraph",
            "---",
            "# LangGraph harness",
        ]),
        encoding="utf-8",
    )
    _write_sidecar(fake_claude, "langgraph-harness", {
        "slug": "langgraph",
        "subject_type": "harness",
        "grade": "A",
        "raw_score": 0.93,
        "hard_floor": "",
    })

    html_out = cm._render_harness_wizard()

    assert "<h1>Harness Setup</h1>" in html_out
    assert "class='setup-flow'" in html_out
    assert "Model -> intent -> install -> attach ctx" in html_out
    assert "id='harness-wizard-form'" in html_out
    assert "Model provider" in html_out
    assert "Development goal" in html_out
    assert "data-harness-slug='langgraph'" in html_out
    assert "ctx-harness-install --recommend" in html_out
    assert "ctx-harness-install langgraph --dry-run" in html_out
    assert "ctx-scan-repo --repo . --recommend" in html_out
    assert "ctx-scan-repo --recommend" not in html_out
    assert "--plan-on-no-fit" in html_out
    assert "ctx attachment" in html_out
    assert "data-testid='harness-command-output'" in html_out


def test_harness_wizard_entries_do_not_scan_full_sidecar_tree(
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness_dir = fake_claude / "skill-wiki" / "entities" / "harnesses"
    harness_dir.mkdir(parents=True)
    (harness_dir / "langgraph.md").write_text(
        "---\ntitle: LangGraph harness\ntype: harness\n---\n# body\n",
        encoding="utf-8",
    )
    _write_sidecar(fake_claude, "langgraph-harness", {
        "slug": "langgraph",
        "subject_type": "harness",
        "grade": "A",
        "raw_score": 0.93,
    })
    monkeypatch.setattr(
        cm,
        "_sidecar_files",
        lambda: (_ for _ in ()).throw(AssertionError("full sidecar scan")),
    )

    entries = cm._harness_wizard_entries()

    assert entries[0]["slug"] == "langgraph"
    assert entries[0]["grade"] == "A"


def test_save_config_updates_casts_values_and_blank_removes_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = tmp_path / ".claude"
    monkeypatch.setattr(cm, "_claude_dir", lambda: claude)
    config_path = claude / "skill-system-config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({
            "resolver": {"recommendation_top_k": 4},
            "skill_transformer": {"line_threshold": 220},
        }),
        encoding="utf-8",
    )

    saved = cm._save_config_updates({
        "resolver.recommendation_top_k": "",
        "skill_transformer.line_threshold": "240",
        "intake.enabled": "false",
        "graph.edge_weights.semantic": "0.65",
    })

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["ok"] is True
    assert "recommendation_top_k" not in raw.get("resolver", {})
    assert raw["skill_transformer"]["line_threshold"] == 240
    assert raw["intake"]["enabled"] is False
    assert raw["graph"]["edge_weights"]["semantic"] == 0.65


def test_render_config_posts_only_dirty_fields_and_can_clear_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = tmp_path / ".claude"
    monkeypatch.setattr(cm, "_claude_dir", lambda: claude)
    config_path = claude / "skill-system-config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"resolver": {"recommendation_top_k": 4}}),
        encoding="utf-8",
    )

    html_out = cm._render_config()

    assert "Saves only changed fields" in html_out
    assert "data-original-value='4'" in html_out
    assert "data-config-clear='resolver.recommendation_top_k'" in html_out
    assert "no config changes to save" in html_out
    assert "el.dataset.originalValue" in html_out


def test_render_graph_landing_shows_seeds_when_available(monkeypatch) -> None:
    """With a non-empty graph, /graph (no slug) should surface popular
    seed slugs when the graph is already cached."""
    import networkx as nx
    G = nx.Graph()
    # High-degree hub: 'skill:python-patterns' with 5 neighbors.
    for peer in ("skill:fastapi-pro", "skill:async-patterns",
                 "agent:code-reviewer", "skill:pydantic", "skill:sqlalchemy"):
        G.add_edge("skill:python-patterns", peer, weight=2)
    G.nodes["skill:python-patterns"]["label"] = "python-patterns"
    monkeypatch.setattr(cm, "_graph_stats", lambda: {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "available": True,
    })
    monkeypatch.setattr(cm, "_GRAPH_CACHE_VALUE", G)

    html_out = cm._render_graph(None)
    # Landing page shows the seeds block.
    assert "Popular seed slugs" in html_out
    # Hub slug must appear as a clickable chip.
    assert "python-patterns" in html_out
    # Graph stats line shows node/edge counts.
    assert "nodes" in html_out


def test_render_graph_landing_does_not_cold_load_graph_for_seed_chips(
    monkeypatch,
) -> None:
    calls = 0

    def fake_load_graph():
        nonlocal calls
        calls += 1
        raise AssertionError("graph cold-loaded")

    monkeypatch.setattr(cm, "_graph_stats", lambda: {
        "nodes": 102697,
        "edges": 2900910,
        "available": True,
    })
    monkeypatch.setattr(cm, "_load_dashboard_graph", fake_load_graph)
    monkeypatch.setattr(cm, "_GRAPH_CACHE_VALUE", None)

    html_out = cm._render_graph(None)

    assert "id='focus'" in html_out
    assert "Popular seed slugs" not in html_out
    assert calls == 0


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
    monkeypatch.setattr(cm, "_GRAPH_CACHE_VALUE", None)
    monkeypatch.setattr(cm, "_GRAPH_CACHE_KEY", None)
    html_out = cm._render_graph(None)
    # No seeds section when graph isn't available.
    assert "Popular seed slugs" not in html_out
    # But the search box and graph list mount still render.
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
