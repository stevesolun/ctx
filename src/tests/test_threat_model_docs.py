from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import tomllib
from typing import Any, cast

import harness_install

from ctx.monitor.pages.loaded import render_loaded
from ctx.monitor.security import (
    MAX_POST_BODY_BYTES,
    READ_TOKEN_COOKIE,
    host_allows_mutations,
    origin_host_name,
    origin_matches_http_host,
    read_token_cookie,
    request_host_name,
)
from ctx.monitor.server import MonitorHandlerDeps, build_monitor_handler


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_threat_model_is_public_and_links_security_policy() -> None:
    threat_model = _read("docs/threat-model.md")
    mkdocs = _read("mkdocs.yml")
    security = _read("SECURITY.md")

    assert "- Threat model: threat-model.md" in mkdocs
    assert "- Enterprise readiness: enterprise-readiness-review.md" in mkdocs
    assert "[SECURITY.md policy]" in threat_model
    assert "github.com/stevesolun/ctx/blob/main/SECURITY.md" in threat_model
    assert "[threat model](docs/threat-model.md)" in security


def test_monitor_docs_match_runtime_exposure_contract() -> None:
    security = _read("SECURITY.md")
    dashboard = _read("docs/dashboard.md")

    assert "--allow-non-loopback" in security
    assert "--allow-non-loopback" in dashboard
    assert "including the effective port" in security
    assert "including the effective" in dashboard
    assert "only with the mutation token" in dashboard
    non_loopback_commands = [
        line
        for line in dashboard.splitlines()
        if "ctx-monitor serve" in line and ("--host 0.0.0.0" in line or "--host ::" in line)
    ]
    assert non_loopback_commands
    assert all("--allow-non-loopback" in line for line in non_loopback_commands)


def test_threat_model_distinguishes_advice_execution_and_isolation() -> None:
    threat_model = _read("docs/threat-model.md")

    assert "Recommendation Is Not Execution" in threat_model
    assert "A recommendation does not run an `install_command`" in threat_model
    assert "does **not** provide process isolation or network isolation" in threat_model
    assert "recommendations, not current ctx features" in threat_model
    assert "does not perform an equivalent post-check of a remotely" in threat_model


def test_threat_model_records_critical_unimplemented_boundaries() -> None:
    threat_model = _read("docs/threat-model.md")

    assert "not a universal ingestion gate" in threat_model
    assert "compares the canonical hostname and" in threat_model
    assert "reads are available to loopback clients without authentication" in threat_model
    assert "embed the token in rendered JavaScript" in threat_model
    assert "not isolation from another" in threat_model
    assert "does not prevent SSRF or DNS rebinding" in threat_model
    assert "does **not** provide process isolation or network isolation" in threat_model


def test_threat_model_records_provenance_and_ephemeral_wiki_contracts() -> None:
    threat_model = _read("docs/threat-model.md")

    assert "binds the complete canonical" in threat_model
    assert "Permission evidence must be a digest-verified checked-in file" in threat_model
    assert "inherited Design.md corpus remains blocked" in threat_model
    assert "raw `ctx__wiki_get` call and result to one" in threat_model
    assert "blocks compaction while raw wiki" in threat_model
    assert "model independently quotes or summarizes" in threat_model


def _monitor_handler(
    *,
    host: str,
    origin: str | None,
    token: str = "monitor-secret",
    mutations_enabled: bool = True,
) -> Any:
    deps = MonitorHandlerDeps(
        monitor_token=lambda: token,
        mutations_enabled_default=lambda: mutations_enabled,
        host_allows_mutations=host_allows_mutations,
        request_host_name=request_host_name,
        origin_host_name=origin_host_name,
        origin_matches_http_host=origin_matches_http_host,
        read_token_cookie=read_token_cookie,
        read_token_cookie_name=READ_TOKEN_COOKIE,
        max_post_body_bytes=MAX_POST_BODY_BYTES,
        audit_log_path=lambda: ROOT / ".unused-monitor-audit.jsonl",
        handle_get_route=lambda _handler, _route, _query: None,
        handle_post_route=lambda _handler, _route, _body, _path: None,
    )
    handler_type = build_monitor_handler(deps)
    handler = cast(Any, object.__new__(handler_type))
    handler.server = SimpleNamespace(_ctx_mutations_enabled=mutations_enabled)
    handler.headers = {"Host": host}
    if origin is not None:
        handler.headers["Origin"] = origin
    return handler


def test_monitor_origin_gate_requires_exact_http_authority() -> None:
    assert (
        _monitor_handler(
            host="localhost:8765",
            origin="http://localhost:8765",
        )._same_origin()
        is True
    )
    assert (
        _monitor_handler(
            host="localhost:8765",
            origin="https://localhost:8765",
        )._same_origin()
        is False
    )
    assert (
        _monitor_handler(
            host="localhost:8765",
            origin="http://localhost:9443",
        )._same_origin()
        is False
    )


def test_loopback_reads_need_no_token_and_mutation_ui_embeds_token() -> None:
    handler = _monitor_handler(host="127.0.0.1:8765", origin=None)

    assert handler._read_authorized({}) is True

    rendered = render_loaded(
        {"load": [], "unload": []},
        mutations_enabled=True,
        monitor_token="rendered-secret",
        layout=lambda _title, body: body,
    )
    assert 'const CTX_MONITOR_TOKEN = "rendered-secret"' in rendered


def test_remote_harness_url_validation_allows_private_network_hosts() -> None:
    for url in (
        "https://127.0.0.1/repository",
        "https://10.0.0.8/repository",
        "https://169.254.169.254/repository",
        "https://rebind.example/repository",
    ):
        harness_install._validate_remote_repo_url(url)


def test_source_registry_is_wired_only_to_designmd_full_body_import() -> None:
    pyproject = tomllib.loads(_read("pyproject.toml"))
    assert pyproject["project"]["scripts"]["ctx-source-registry"] == "ctx.core.source_registry:main"

    consumers: list[str] = []
    source_root = ROOT / "src"
    registry_path = source_root / "ctx" / "core" / "source_registry.py"
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root)
        if relative.parts[0] == "tests" or path == registry_path:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "ctx.core.source_registry" for alias in node.names
            ):
                consumers.append(str(relative))
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("source_registry"):
                consumers.append(str(relative))

    assert consumers == ["import_designdotmd_skills.py"]
