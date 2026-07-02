from __future__ import annotations

import gzip
import json
import tarfile
from io import BytesIO
from pathlib import Path

from ctx.core.quality.skillspector_audit import (
    SKILLSPECTOR_REPO_URL,
    STAMP_BEGIN,
    SkillSpectorAuditRecord,
    _record_from_report,
    cover_entity_pages,
    load_audit_records,
    stamp_directory,
    stamp_entity_text,
    stamp_tar,
    summarize_audit,
)


def _record(slug: str = "demo") -> SkillSpectorAuditRecord:
    return SkillSpectorAuditRecord(
        schema_version=1,
        slug=slug,
        status="passed",
        risk_score=0,
        risk_severity="LOW",
        recommendation="SAFE",
        issues=0,
        components=2,
        content_sha256="abc123",
        scanned_at="2026-06-17T00:00:00+00:00",
        scanner="NVIDIA SkillSpector",
        scanner_repo=SKILLSPECTOR_REPO_URL,
        scanner_version="2.2.3",
        mode="static-no-llm",
        llm_requested=False,
        elapsed_seconds=0.2,
    )


def _add_text(tf: tarfile.TarFile, name: str, text: str) -> None:
    payload = text.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o644
    tf.addfile(info, BytesIO(payload))


def _write_audit(path: Path, *records: SkillSpectorAuditRecord) -> None:
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record.to_json(), sort_keys=True))
            f.write("\n")


def test_record_from_skillspector_report_normalizes_safe_result() -> None:
    report = {
        "skill": {"scanned_at": "2026-06-17T01:02:03+00:00"},
        "risk_assessment": {
            "score": 0,
            "severity": "LOW",
            "recommendation": "SAFE",
        },
        "components": [{"path": "SKILL.md"}, {"path": "references/a.md"}],
        "issues": [{"id": "E1"}],
        "metadata": {
            "skillspector_version": "2.2.3",
            "llm_requested": False,
        },
    }

    record = _record_from_report(
        "demo",
        report,
        content_sha256="abc",
        elapsed_seconds=0.123,
    )

    assert record.status == "findings"
    assert record.risk_score == 0
    assert record.components == 2
    assert record.issues == 1
    assert record.issue_rules == ("E1",)
    assert record.scanner_repo == SKILLSPECTOR_REPO_URL


def test_stamp_entity_text_is_visible_and_idempotent() -> None:
    text = '---\ntitle: Demo\nskillspector_status: "old"\n---\n# Demo\n'
    once = stamp_entity_text(text, _record())
    twice = stamp_entity_text(once, _record())

    assert once == twice
    assert "skillspector_checked: true" in once
    assert 'skillspector_status: "passed"' in once
    assert "not NVIDIA endorsement" in once
    assert once.count(STAMP_BEGIN) == 1


def test_stamp_tar_updates_skill_entities_and_embeds_audit(tmp_path: Path) -> None:
    source = tmp_path / "wiki.tar.gz"
    audit = tmp_path / "audit.jsonl.gz"
    out = tmp_path / "wiki-stamped.tar.gz"
    _write_audit(audit, _record("demo"))
    with tarfile.open(source, "w:gz") as tf:
        _add_text(tf, "entities/skills/demo.md", "---\ntitle: Demo\n---\n# Demo\n")
        _add_text(tf, "entities/skills/other.md", "---\ntitle: Other\n---\n# Other\n")
        _add_text(tf, "converted/demo/SKILL.md", "# Demo skill\n")

    stats = stamp_tar(source, audit, out)

    assert stats["stamped"] == 1
    assert stats["audit_records"] == 1
    with tarfile.open(out, "r:gz") as tf:
        stamped = tf.extractfile("entities/skills/demo.md")
        assert stamped is not None
        text = stamped.read().decode("utf-8")
        assert "skillspector_checked: true" in text
        assert "NVIDIA SkillSpector" in text
        unchanged = tf.extractfile("entities/skills/other.md")
        assert unchanged is not None
        assert "skillspector_checked" not in unchanged.read().decode("utf-8")
        audit_member = tf.extractfile("security/skillspector-audit.jsonl.gz")
        assert audit_member is not None

    loaded = load_audit_records(audit)
    assert list(loaded) == ["demo"]
    assert summarize_audit(audit)["records"] == 1


def test_stamp_directory_updates_only_audited_entities(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    (wiki / "entities" / "skills").mkdir(parents=True)
    (wiki / "entities" / "skills" / "demo.md").write_text(
        "---\ntitle: Demo\n---\n# Demo\n",
        encoding="utf-8",
    )
    (wiki / "entities" / "skills" / "other.md").write_text(
        "---\ntitle: Other\n---\n# Other\n",
        encoding="utf-8",
    )
    audit = tmp_path / "audit.jsonl.gz"
    _write_audit(audit, _record("demo"))

    stats = stamp_directory(wiki, audit)

    assert stats == {"stamped": 1, "missing": 0, "audit_records": 1}
    assert "skillspector_checked: true" in (wiki / "entities" / "skills" / "demo.md").read_text(
        encoding="utf-8"
    )
    assert "skillspector_checked" not in (wiki / "entities" / "skills" / "other.md").read_text(
        encoding="utf-8"
    )
    assert (wiki / "security" / "skillspector-audit.jsonl.gz").exists()


def test_cover_entity_pages_appends_no_body_records(tmp_path: Path) -> None:
    source = tmp_path / "wiki.tar.gz"
    audit = tmp_path / "audit.jsonl.gz"
    _write_audit(audit, _record("with-body"))
    with tarfile.open(source, "w:gz") as tf:
        _add_text(tf, "entities/skills/with-body.md", "# With body\n")
        _add_text(tf, "entities/skills/no-body.md", "# No body\n")
        _add_text(tf, "converted/with-body/SKILL.md", "# With body\n")

    stats = cover_entity_pages(source, audit)

    assert stats == {
        "entity_pages": 2,
        "converted_bodies": 1,
        "missing_bodies": 1,
        "appended": 1,
    }
    records = load_audit_records(audit)
    assert records["no-body"].status == "not_scanned_no_body"
    stamped = stamp_entity_text("# No body\n", records["no-body"])
    assert "not scanned by" in stamped
    assert "has no converted `SKILL.md` body" in stamped
