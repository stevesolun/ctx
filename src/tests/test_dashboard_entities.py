from __future__ import annotations

from pathlib import Path
from typing import Literal

from ctx import dashboard_entities
from ctx.core.quality.skillspector_service import SkillSpectorResult
from ctx.monitor import testing as monitor_testing


class _NoopLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> Literal[False]:
        return False


def test_skill_upsert_requires_security_gate_before_write(tmp_path: Path) -> None:
    writes: list[tuple[Path, str]] = []
    queued: list[tuple[str, str, Path, str, str]] = []

    def fail_scan(_slug: str, _content: str) -> tuple[bool, str]:
        return False, "SkillSpector security scan did not pass: findings"

    deps = dashboard_entities.EntityCrudDeps(
        is_safe_slug=lambda value: value == "unsafe-skill",
        normalize_entity_type=lambda value: "skill" if value == "skill" else None,
        wiki_entity_detail=lambda _slug, _etype: None,
        wiki_entity_target_path=lambda slug, _etype: tmp_path / f"{slug}.md",
        wiki_entity_path=lambda _slug, _etype: None,
        iter_wiki_entity_paths=lambda _etype: [],
        read_manifest=lambda: {"load": []},
        perform_unload=lambda _slug, _etype: (True, "unloaded"),
        queue_entity_refresh=lambda *args: queued.append(args),  # type: ignore[arg-type]
        file_lock=lambda _path: _NoopLock(),
        write_entity_text=lambda path, content: writes.append((path, content)),
        parse_frontmatter=lambda text: ({}, text),
        frontmatter_tags=lambda _value: [],
        frontmatter_text=lambda value: str(value or ""),
        display_slug=lambda value: value,
        display_label=lambda value: str(value),
        entity_wiki_href=lambda slug, _etype: f"/wiki/{slug}?type=skill",
        scan_skill_content=fail_scan,
    )

    ok, detail = dashboard_entities.upsert_wiki_entity(
        {
            "slug": "unsafe-skill",
            "entity_type": "skill",
            "title": "Unsafe Skill",
            "body": "# Unsafe\n",
        },
        deps=deps,
    )

    assert ok is False
    assert "SkillSpector" in detail
    assert writes == []
    assert queued == []


def test_monitor_entity_deps_scan_manual_skill_upserts(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    def fake_scan(slug: str, content: str) -> SkillSpectorResult:
        seen.append((slug, content))
        return SkillSpectorResult(
            status="findings",
            command=["skillspector", "scan"],
            exit_code=1,
            output="prompt injection",
        )

    monkeypatch.setattr(monitor_testing, "run_skillspector_scan_text", fake_scan)

    ok, detail = monitor_testing.scan_skill_entity_content("unsafe-skill", "# Unsafe\n")

    assert ok is False
    assert "SkillSpector security scan did not pass: findings" in detail
    assert seen == [("unsafe-skill", "# Unsafe\n")]
