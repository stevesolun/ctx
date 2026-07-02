"""Batch SkillSpector audit support for shipped ctx skill wiki artifacts.

This module intentionally keeps SkillSpector as an optional external runtime.
ctx supports Python 3.11, while SkillSpector currently requires Python 3.12+.
Run this file under a SkillSpector-enabled interpreter, for example:

    uv run --no-project --python 3.12 \
      --with git+https://github.com/NVIDIA/skillspector \
      python src/ctx/core/quality/skillspector_audit.py audit-tar \
      --wiki-tar graph/wiki-graph.tar.gz \
      --out graph/skillspector-audit.jsonl.gz

The audit is a ctx-run check using NVIDIA's Apache-2.0 SkillSpector tool. It
must not be represented as NVIDIA endorsement, certification, or signature.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import gzip
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, IO, Iterable, TextIO, cast

SKILLSPECTOR_REPO_URL = "https://github.com/NVIDIA/SkillSpector"
AUDIT_SCHEMA_VERSION = 1
STAMP_BEGIN = "<!-- ctx-skillspector:begin -->"
STAMP_END = "<!-- ctx-skillspector:end -->"
DEFAULT_AUDIT_MEMBER = "security/skillspector-audit.jsonl.gz"
MAX_PYTHON_TAR_STAMP_MB = 64

_SAFE_ENV_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "PATHEXT",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "VIRTUAL_ENV",
    "WINDIR",
}


@dataclass(frozen=True)
class SkillSpectorAuditRecord:
    """Compact persisted audit result for one converted skill body."""

    schema_version: int
    slug: str
    status: str
    risk_score: int | None
    risk_severity: str | None
    recommendation: str | None
    issues: int
    components: int
    content_sha256: str | None
    scanned_at: str
    scanner: str
    scanner_repo: str
    scanner_version: str | None
    mode: str
    llm_requested: bool
    elapsed_seconds: float | None = None
    error: str | None = None
    issue_rules: tuple[str, ...] = ()

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["issue_rules"] = list(self.issue_rules)
        return payload


def _safe_tar_name(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    first = parts[0]
    if (
        normalized.startswith("/")
        or (len(first) == 2 and first[1] == ":")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return None
    return normalized


def _converted_slug(name: str) -> str | None:
    safe = _safe_tar_name(name)
    if safe is None or not safe.startswith("converted/"):
        return None
    parts = safe.split("/")
    if len(parts) < 3:
        return None
    slug = parts[1]
    if not slug or slug in {".", ".."}:
        return None
    return slug


def _entity_skill_slug(name: str) -> str | None:
    safe = _safe_tar_name(name)
    if safe is None or not safe.startswith("entities/skills/") or not safe.endswith(".md"):
        return None
    slug = safe.removeprefix("entities/skills/").removesuffix(".md")
    if "/" in slug or "\\" in slug or not slug:
        return None
    return slug


def _copy_stream(src: IO[bytes], dst: IO[bytes], chunk_size: int = 1024 * 1024) -> None:
    while True:
        chunk = src.read(chunk_size)
        if not chunk:
            return
        dst.write(chunk)


def _write_jsonl_gz(
    path: Path, records: Iterable[SkillSpectorAuditRecord], *, append: bool
) -> None:
    mode = "at" if append and path.exists() else "wt"
    with cast(TextIO, gzip.open(path, mode, encoding="utf-8", newline="\n")) as f:
        for record in records:
            f.write(json.dumps(record.to_json(), sort_keys=True, separators=(",", ":")))
            f.write("\n")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(str(value))


def _int_value(value: object, default: int) -> int:
    if value is None:
        return default
    return int(str(value))


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(str(value))


def load_audit_records(path: Path) -> dict[str, SkillSpectorAuditRecord]:
    records: dict[str, SkillSpectorAuditRecord] = {}
    if not path.exists():
        return records
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid audit JSON at {path}:{line_number}: {exc}") from exc
            records[str(payload["slug"])] = SkillSpectorAuditRecord(
                schema_version=int(payload.get("schema_version") or AUDIT_SCHEMA_VERSION),
                slug=str(payload["slug"]),
                status=str(payload.get("status") or "error"),
                risk_score=(
                    int(payload["risk_score"]) if payload.get("risk_score") is not None else None
                ),
                risk_severity=(
                    str(payload["risk_severity"])
                    if payload.get("risk_severity") is not None
                    else None
                ),
                recommendation=(
                    str(payload["recommendation"])
                    if payload.get("recommendation") is not None
                    else None
                ),
                issues=int(payload.get("issues") or 0),
                components=int(payload.get("components") or 0),
                content_sha256=(
                    str(payload["content_sha256"])
                    if payload.get("content_sha256") is not None
                    else None
                ),
                scanned_at=str(payload.get("scanned_at") or ""),
                scanner=str(payload.get("scanner") or "NVIDIA SkillSpector"),
                scanner_repo=str(payload.get("scanner_repo") or SKILLSPECTOR_REPO_URL),
                scanner_version=(
                    str(payload["scanner_version"])
                    if payload.get("scanner_version") is not None
                    else None
                ),
                mode=str(payload.get("mode") or "static-no-llm"),
                llm_requested=bool(payload.get("llm_requested")),
                elapsed_seconds=(
                    float(payload["elapsed_seconds"])
                    if payload.get("elapsed_seconds") is not None
                    else None
                ),
                error=str(payload["error"]) if payload.get("error") else None,
                issue_rules=tuple(str(rule) for rule in payload.get("issue_rules") or ()),
            )
    return records


def _skill_content_hash(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in skill_dir.rglob("*") if p.is_file()):
        relative = path.relative_to(skill_dir).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _sanitize_worker_env() -> None:
    safe = {key: value for key, value in os.environ.items() if key.upper() in _SAFE_ENV_KEYS}
    os.environ.clear()
    os.environ.update(safe)


def _record_from_report(
    slug: str,
    report: dict[str, Any],
    *,
    content_sha256: str | None,
    elapsed_seconds: float | None,
) -> SkillSpectorAuditRecord:
    risk = report.get("risk_assessment") if isinstance(report, dict) else {}
    metadata = report.get("metadata") if isinstance(report, dict) else {}
    issues = report.get("issues") if isinstance(report, dict) else []
    components = report.get("components") if isinstance(report, dict) else []
    score = risk.get("score") if isinstance(risk, dict) else None
    severity = risk.get("severity") if isinstance(risk, dict) else None
    recommendation = risk.get("recommendation") if isinstance(risk, dict) else None
    issue_rules = []
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            rule = issue.get("rule_id") or issue.get("id")
            if rule:
                issue_rules.append(str(rule))
    status = "passed"
    if isinstance(issues, list) and issues:
        status = "findings"
    if isinstance(score, int | float) and score > 50:
        status = "blocked"
    scanned_at = ""
    skill = report.get("skill") if isinstance(report, dict) else {}
    if isinstance(skill, dict) and skill.get("scanned_at"):
        scanned_at = str(skill["scanned_at"])
    if not scanned_at:
        scanned_at = datetime.now(UTC).isoformat()
    return SkillSpectorAuditRecord(
        schema_version=AUDIT_SCHEMA_VERSION,
        slug=slug,
        status=status,
        risk_score=int(score) if score is not None else None,
        risk_severity=str(severity) if severity is not None else None,
        recommendation=str(recommendation) if recommendation is not None else None,
        issues=len(issues) if isinstance(issues, list) else 0,
        components=len(components) if isinstance(components, list) else 0,
        content_sha256=content_sha256,
        scanned_at=scanned_at,
        scanner="NVIDIA SkillSpector",
        scanner_repo=SKILLSPECTOR_REPO_URL,
        scanner_version=(
            str(metadata["skillspector_version"])
            if isinstance(metadata, dict) and metadata.get("skillspector_version")
            else None
        ),
        mode="static-no-llm",
        llm_requested=bool(metadata.get("llm_requested")) if isinstance(metadata, dict) else False,
        elapsed_seconds=elapsed_seconds,
        issue_rules=tuple(sorted(set(issue_rules))),
    )


def _error_record(
    slug: str, message: str, *, elapsed_seconds: float | None = None
) -> dict[str, object]:
    return SkillSpectorAuditRecord(
        schema_version=AUDIT_SCHEMA_VERSION,
        slug=slug,
        status="error",
        risk_score=None,
        risk_severity=None,
        recommendation=None,
        issues=0,
        components=0,
        content_sha256=None,
        scanned_at=datetime.now(UTC).isoformat(),
        scanner="NVIDIA SkillSpector",
        scanner_repo=SKILLSPECTOR_REPO_URL,
        scanner_version=None,
        mode="static-no-llm",
        llm_requested=False,
        elapsed_seconds=elapsed_seconds,
        error=message,
    ).to_json()


def _no_body_record(slug: str) -> SkillSpectorAuditRecord:
    return SkillSpectorAuditRecord(
        schema_version=AUDIT_SCHEMA_VERSION,
        slug=slug,
        status="not_scanned_no_body",
        risk_score=None,
        risk_severity=None,
        recommendation=None,
        issues=0,
        components=0,
        content_sha256=None,
        scanned_at=datetime.now(UTC).isoformat(),
        scanner="NVIDIA SkillSpector",
        scanner_repo=SKILLSPECTOR_REPO_URL,
        scanner_version=None,
        mode="not-run-no-body",
        llm_requested=False,
        error="No converted SKILL.md body is shipped for this skill entity.",
    )


def _scan_skill_dir(skill_dir_str: str) -> dict[str, object]:
    skill_dir = Path(skill_dir_str)
    slug = skill_dir.name
    started = time.perf_counter()
    try:
        from skillspector.graph import graph  # type: ignore[import-not-found]

        content_sha256 = _skill_content_hash(skill_dir)
        result = graph.invoke(
            {
                "input_path": str(skill_dir),
                "output_format": "json",
                "use_llm": False,
            }
        )
        report_body = result.get("report_body") if isinstance(result, dict) else None
        report = json.loads(str(report_body or "{}"))
        record = _record_from_report(
            slug,
            report,
            content_sha256=content_sha256,
            elapsed_seconds=round(time.perf_counter() - started, 3),
        )
        return record.to_json()
    except Exception as exc:  # noqa: BLE001 - scanner failures become audit records.
        return _error_record(
            slug, str(exc), elapsed_seconds=round(time.perf_counter() - started, 3)
        )


def _extract_member(member: tarfile.TarInfo, tf: tarfile.TarFile, dest_root: Path) -> None:
    safe = _safe_tar_name(member.name)
    if safe is None:
        raise ValueError(f"unsafe tar member: {member.name!r}")
    parts = safe.split("/")
    relative = Path(*parts[2:])
    dest = dest_root / parts[1] / relative
    if not str(dest.resolve()).startswith(str(dest_root.resolve())):
        raise ValueError(f"unsafe extraction target: {member.name!r}")
    if member.isdir():
        dest.mkdir(parents=True, exist_ok=True)
        return
    if not member.isfile():
        return
    src = tf.extractfile(member)
    if src is None:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src, dest.open("wb") as out:
        _copy_stream(src, out)
    try:
        # Some upstream archives carry restrictive modes. Preserve executable
        # bits where present, but force owner read/write so the isolated
        # SkillSpector worker can inspect the extracted skill body.
        dest.chmod((member.mode & 0o777) | 0o600)
    except OSError:
        pass


def _completed_record_from_payload(payload: dict[str, object]) -> SkillSpectorAuditRecord:
    issue_rules = payload.get("issue_rules")
    if not isinstance(issue_rules, list | tuple):
        issue_rules = ()
    return SkillSpectorAuditRecord(
        schema_version=_int_value(payload.get("schema_version"), AUDIT_SCHEMA_VERSION),
        slug=str(payload["slug"]),
        status=str(payload.get("status") or "error"),
        risk_score=_optional_int(payload.get("risk_score")),
        risk_severity=str(payload["risk_severity"]) if payload.get("risk_severity") else None,
        recommendation=str(payload["recommendation"]) if payload.get("recommendation") else None,
        issues=_int_value(payload.get("issues"), 0),
        components=_int_value(payload.get("components"), 0),
        content_sha256=str(payload["content_sha256"]) if payload.get("content_sha256") else None,
        scanned_at=str(payload.get("scanned_at") or datetime.now(UTC).isoformat()),
        scanner=str(payload.get("scanner") or "NVIDIA SkillSpector"),
        scanner_repo=str(payload.get("scanner_repo") or SKILLSPECTOR_REPO_URL),
        scanner_version=str(payload["scanner_version"]) if payload.get("scanner_version") else None,
        mode=str(payload.get("mode") or "static-no-llm"),
        llm_requested=bool(payload.get("llm_requested")),
        elapsed_seconds=(_optional_float(payload.get("elapsed_seconds"))),
        error=str(payload["error"]) if payload.get("error") else None,
        issue_rules=tuple(str(rule) for rule in issue_rules),
    )


def audit_tar(
    wiki_tar: Path,
    out: Path,
    *,
    workers: int,
    limit: int | None = None,
    resume: bool = True,
    temp_dir: Path | None = None,
    progress_every: int = 1000,
) -> dict[str, int]:
    """Stream converted skill bodies from ``wiki_tar`` and write compact audit records."""
    completed = load_audit_records(out) if resume else {}
    out.parent.mkdir(parents=True, exist_ok=True)
    append = resume and out.exists()
    submitted = 0
    completed_count = 0
    skipped = 0
    errors = 0
    pending: dict[concurrent.futures.Future[dict[str, object]], Path] = {}
    max_pending = max(workers * 2, 1)
    closed_slugs: set[str] = set()

    def drain_one() -> None:
        nonlocal completed_count, errors, append
        done, _ = concurrent.futures.wait(
            pending,
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        for future in done:
            skill_dir = pending.pop(future)
            try:
                payload = future.result()
                record = _completed_record_from_payload(payload)
            except Exception as exc:  # noqa: BLE001
                record = _completed_record_from_payload(_error_record(skill_dir.name, str(exc)))
                errors += 1
            else:
                if record.status == "error":
                    errors += 1
            _write_jsonl_gz(out, [record], append=append)
            append = True
            completed_count += 1
            if progress_every > 0 and completed_count % progress_every == 0:
                print(
                    json.dumps(
                        {
                            "event": "progress",
                            "completed": completed_count,
                            "errors": errors,
                            "submitted": submitted,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            shutil.rmtree(skill_dir, ignore_errors=True)

    with tempfile.TemporaryDirectory(prefix="ctx-skillspector-audit-", dir=temp_dir) as work:
        work_root = Path(work)
        current_slug: str | None = None
        current_root: Path | None = None
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=max(workers, 1),
            initializer=_sanitize_worker_env,
        ) as pool:
            with tarfile.open(wiki_tar, "r:gz") as tf:
                for member in tf:
                    slug = _converted_slug(member.name)
                    if slug is None:
                        continue
                    if current_slug is not None and slug != current_slug:
                        if current_root is not None and (current_root / "SKILL.md").exists():
                            pending[pool.submit(_scan_skill_dir, str(current_root))] = current_root
                            submitted += 1
                            if limit is not None and submitted >= limit:
                                break
                            while len(pending) >= max_pending:
                                drain_one()
                        closed_slugs.add(current_slug)
                        current_slug = None
                        current_root = None
                    if slug in completed:
                        skipped += 1 if member.name.endswith("/SKILL.md") else 0
                        continue
                    if slug in closed_slugs:
                        raise ValueError(
                            f"tar is not grouped by converted skill; slug reopened: {slug}"
                        )
                    if current_slug is None:
                        current_slug = slug
                        current_root = work_root / slug
                    _extract_member(member, tf, work_root)
                else:
                    if current_slug is not None and current_root is not None:
                        if current_root.exists() and (current_root / "SKILL.md").exists():
                            if limit is None or submitted < limit:
                                pending[pool.submit(_scan_skill_dir, str(current_root))] = (
                                    current_root
                                )
                                submitted += 1
            while pending:
                drain_one()

    return {
        "submitted": submitted,
        "completed": completed_count,
        "skipped": len(completed),
        "errors": errors,
    }


def _quote_yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _stamp_block(record: SkillSpectorAuditRecord) -> str:
    score = "unknown" if record.risk_score is None else str(record.risk_score)
    severity = record.risk_severity or "UNKNOWN"
    recommendation = record.recommendation or "UNKNOWN"
    version = record.scanner_version or "unknown"
    if record.status == "not_scanned_no_body":
        return (
            f"{STAMP_BEGIN}\n"
            f"> Security check: not scanned by "
            f"[NVIDIA SkillSpector]({record.scanner_repo}) because this generated "
            f"skill entity has no converted `SKILL.md` body in the shipped wiki. "
            f"This is a ctx coverage marker, not an NVIDIA endorsement or "
            f"certification.\n"
            f"{STAMP_END}\n"
        )
    if record.status == "error":
        return (
            f"{STAMP_BEGIN}\n"
            f"> Security check: attempted with "
            f"[NVIDIA SkillSpector]({record.scanner_repo}) ({record.mode}) but "
            f"the scan errored: {record.error or 'unknown error'}. This is a "
            f"ctx-run tool check, not an NVIDIA endorsement or certification.\n"
            f"{STAMP_END}\n"
        )
    return (
        f"{STAMP_BEGIN}\n"
        f"> Security check: checked with "
        f"[NVIDIA SkillSpector]({record.scanner_repo}) v{version} "
        f"({record.mode}). Result: **{record.status}**; risk {severity}/{score}; "
        f"recommendation {recommendation}; findings {record.issues}; "
        f"components {record.components}. This is a ctx-run tool check, not an "
        f"NVIDIA endorsement or certification.\n"
        f"{STAMP_END}\n"
    )


def stamp_entity_text(text: str, record: SkillSpectorAuditRecord) -> str:
    """Return entity markdown stamped with compact SkillSpector metadata."""
    stripped = _remove_stamp_block(text)
    body = stripped
    frontmatter = ""
    if stripped.startswith("---\n"):
        end = stripped.find("\n---\n", 4)
        if end != -1:
            frontmatter = stripped[4:end]
            body = stripped[end + 5 :]
    lines = [line for line in frontmatter.splitlines() if not line.startswith("skillspector_")]
    lines.extend(
        [
            "skillspector_checked: true",
            f"skillspector_status: {_quote_yaml(record.status)}",
            f"skillspector_risk_score: {record.risk_score if record.risk_score is not None else 'null'}",
            f"skillspector_risk_severity: {_quote_yaml(record.risk_severity or 'UNKNOWN')}",
            f"skillspector_issues: {record.issues}",
            f"skillspector_components: {record.components}",
            f"skillspector_version: {_quote_yaml(record.scanner_version or 'unknown')}",
            f"skillspector_mode: {_quote_yaml(record.mode)}",
            f"skillspector_repo: {_quote_yaml(record.scanner_repo)}",
            f"skillspector_checked_at: {_quote_yaml(record.scanned_at)}",
            f"skillspector_note: {_quote_yaml('ctx-run SkillSpector check; not NVIDIA endorsement')}",
        ]
    )
    stamped = "---\n" + "\n".join(lines).rstrip() + "\n---\n"
    return stamped + "\n" + _stamp_block(record) + "\n" + body.lstrip()


def _remove_stamp_block(text: str) -> str:
    start = text.find(STAMP_BEGIN)
    if start == -1:
        return text
    end = text.find(STAMP_END, start)
    if end == -1:
        return text[:start].rstrip() + "\n"
    return (text[:start] + text[end + len(STAMP_END) :]).lstrip("\n")


def _add_bytes(tf: tarfile.TarFile, template: tarfile.TarInfo, payload: bytes) -> None:
    info = tarfile.TarInfo(template.name)
    info.size = len(payload)
    info.mode = template.mode
    info.mtime = template.mtime
    info.uid = template.uid
    info.gid = template.gid
    info.uname = template.uname
    info.gname = template.gname
    tf.addfile(info, fileobj=_BytesReader(payload))


class _BytesReader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._payload) - self._offset
        end = min(self._offset + size, len(self._payload))
        chunk = self._payload[self._offset : end]
        self._offset = end
        return chunk


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def stamp_directory(
    wiki_dir: Path,
    audit: Path,
    *,
    audit_member: str = DEFAULT_AUDIT_MEMBER,
) -> dict[str, int]:
    """Stamp an extracted wiki directory.

    This is the release path for the full ctx wiki. It touches only skill entity
    pages that have audit records, then the existing native tar repack flow can
    refresh ``graph/wiki-graph.tar.gz`` quickly.
    """
    records = load_audit_records(audit)
    stamped = 0
    missing = 0
    entities_dir = wiki_dir / "entities" / "skills"
    for slug, record in records.items():
        path = entities_dir / f"{slug}.md"
        if not path.exists():
            missing += 1
            continue
        text = path.read_text(encoding="utf-8")
        _atomic_write_text(path, stamp_entity_text(text, record))
        stamped += 1
    audit_path = wiki_dir / Path(*audit_member.split("/"))
    _atomic_write_bytes(audit_path, audit.read_bytes())
    return {"stamped": stamped, "missing": missing, "audit_records": len(records)}


def stamp_tar(
    wiki_tar: Path,
    audit: Path,
    out: Path,
    *,
    audit_member: str = DEFAULT_AUDIT_MEMBER,
    allow_large_python_repack: bool = False,
) -> dict[str, int]:
    tar_mb = wiki_tar.stat().st_size / (1024 * 1024)
    if not allow_large_python_repack and tar_mb > MAX_PYTHON_TAR_STAMP_MB:
        raise ValueError(
            "stamp-tar uses Python gzip tar rewriting and is intended for small artifacts. "
            "For the release wiki, extract the wiki, run stamp-dir, then use the native "
            f"tar repack flow. Refusing to rewrite {tar_mb:.1f} MiB without "
            "--allow-large-python-repack."
        )
    records = load_audit_records(audit)
    stamped = 0
    copied = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(wiki_tar, "r:gz") as src_tf, tarfile.open(out, "w:gz") as dst_tf:
        for member in src_tf:
            slug = _entity_skill_slug(member.name)
            if slug is not None and slug in records and member.isfile():
                f = src_tf.extractfile(member)
                if f is None:
                    continue
                with f:
                    text = f.read().decode("utf-8")
                payload = stamp_entity_text(text, records[slug]).encode("utf-8")
                _add_bytes(dst_tf, member, payload)
                stamped += 1
                continue
            dst_tf.addfile(member, src_tf.extractfile(member) if member.isfile() else None)
            copied += 1
        audit_payload = audit.read_bytes()
        info = tarfile.TarInfo(audit_member)
        info.size = len(audit_payload)
        info.mode = 0o644
        info.mtime = int(time.time())
        dst_tf.addfile(info, fileobj=_BytesReader(audit_payload))
    return {"stamped": stamped, "copied": copied, "audit_records": len(records)}


def summarize_audit(path: Path) -> dict[str, object]:
    records = load_audit_records(path)
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    max_score = 0
    for record in records.values():
        by_status[record.status] = by_status.get(record.status, 0) + 1
        severity = record.risk_severity or "UNKNOWN"
        by_severity[severity] = by_severity.get(severity, 0) + 1
        if record.risk_score is not None:
            max_score = max(max_score, record.risk_score)
    return {
        "records": len(records),
        "by_status": dict(sorted(by_status.items())),
        "by_severity": dict(sorted(by_severity.items())),
        "max_score": max_score,
        "scanner_repo": SKILLSPECTOR_REPO_URL,
    }


def cover_entity_pages(wiki_tar: Path, audit: Path) -> dict[str, int]:
    """Append honest coverage records for skill entities without converted bodies."""
    records = load_audit_records(audit)
    entity_slugs: set[str] = set()
    converted_slugs: set[str] = set()
    with tarfile.open(wiki_tar, "r:gz") as tf:
        for member in tf:
            safe_name = _safe_tar_name(member.name)
            if safe_name is None:
                continue
            entity_slug = _entity_skill_slug(safe_name)
            if entity_slug is not None:
                entity_slugs.add(entity_slug)
            converted_slug = _converted_slug(safe_name)
            if converted_slug is not None and safe_name.endswith("/SKILL.md"):
                converted_slugs.add(converted_slug)
    missing_body = sorted(entity_slugs - converted_slugs)
    to_append = [_no_body_record(slug) for slug in missing_body if slug not in records]
    if to_append:
        _write_jsonl_gz(audit, to_append, append=True)
    return {
        "entity_pages": len(entity_slugs),
        "converted_bodies": len(converted_slugs),
        "missing_bodies": len(missing_body),
        "appended": len(to_append),
    }


def _audit_tar_command(args: argparse.Namespace) -> int:
    stats = audit_tar(
        Path(args.wiki_tar),
        Path(args.out),
        workers=args.workers,
        limit=args.limit,
        resume=not args.no_resume,
        temp_dir=Path(args.temp_dir) if args.temp_dir else None,
        progress_every=args.progress_every,
    )
    print(json.dumps(stats, sort_keys=True))
    return 1 if stats["errors"] else 0


def _stamp_tar_command(args: argparse.Namespace) -> int:
    try:
        stats = stamp_tar(
            Path(args.wiki_tar),
            Path(args.audit),
            Path(args.out),
            allow_large_python_repack=args.allow_large_python_repack,
        )
    except ValueError as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(stats, sort_keys=True))
    return 0


def _stamp_dir_command(args: argparse.Namespace) -> int:
    stats = stamp_directory(Path(args.wiki_dir), Path(args.audit))
    print(json.dumps(stats, sort_keys=True))
    return 0


def _summary_command(args: argparse.Namespace) -> int:
    print(json.dumps(summarize_audit(Path(args.audit)), indent=2, sort_keys=True))
    return 0


def _cover_entities_command(args: argparse.Namespace) -> int:
    stats = cover_entity_pages(Path(args.wiki_tar), Path(args.audit))
    print(json.dumps(stats, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit/stamp ctx skill wiki artifacts with SkillSpector."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser(
        "audit-tar", help="Scan converted skill bodies from a wiki tarball."
    )
    audit_parser.add_argument("--wiki-tar", required=True, help="Path to graph/wiki-graph.tar.gz.")
    audit_parser.add_argument("--out", required=True, help="Audit JSONL gzip output path.")
    audit_parser.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) // 2, 1))
    audit_parser.add_argument("--limit", type=int, default=None, help="Optional pilot limit.")
    audit_parser.add_argument("--no-resume", action="store_true", help="Ignore existing output.")
    audit_parser.add_argument("--temp-dir", default=None, help="Optional parent temp directory.")
    audit_parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print a JSON progress line every N completed scans; 0 disables.",
    )
    audit_parser.set_defaults(func=_audit_tar_command)

    stamp_parser = subparsers.add_parser(
        "stamp-tar", help="Stamp skill entity pages using an audit file."
    )
    stamp_parser.add_argument("--wiki-tar", required=True)
    stamp_parser.add_argument("--audit", required=True)
    stamp_parser.add_argument("--out", required=True)
    stamp_parser.add_argument(
        "--allow-large-python-repack",
        action="store_true",
        help="Allow slow Python gzip rewriting for large tarballs.",
    )
    stamp_parser.set_defaults(func=_stamp_tar_command)

    stamp_dir_parser = subparsers.add_parser(
        "stamp-dir",
        help="Stamp skill entity pages in an extracted wiki directory.",
    )
    stamp_dir_parser.add_argument("--wiki-dir", required=True)
    stamp_dir_parser.add_argument("--audit", required=True)
    stamp_dir_parser.set_defaults(func=_stamp_dir_command)

    summary_parser = subparsers.add_parser("summary", help="Summarize audit JSONL gzip.")
    summary_parser.add_argument("--audit", required=True)
    summary_parser.set_defaults(func=_summary_command)

    cover_parser = subparsers.add_parser(
        "cover-entities",
        help="Append no-body coverage records for skill entity pages without SKILL.md bodies.",
    )
    cover_parser.add_argument("--wiki-tar", required=True)
    cover_parser.add_argument("--audit", required=True)
    cover_parser.set_defaults(func=_cover_entities_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
