from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

import import_designdotmd_skills as importer
import ctx.core.source_registry as source_registry
from ctx.core.source_registry import (
    ExternalSourceRecord,
    canonical_ingestion_manifest_sha256,
)


SOURCE_TEXT = """---
name: Fixture Design
description: A fixture design system
typography:
  tags: nested-metadata
---
# Fixture Design

Keep this body unchanged.
"""


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


@dataclass
class DesignImportFixture:
    root: Path
    import_root: Path
    manifest_path: Path
    source: Path
    target: Path
    manifest: dict[str, Any]
    entry: dict[str, Any]


@pytest.fixture
def design_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> DesignImportFixture:
    import_root = tmp_path / "imported-skills" / "designdotmd"
    source = import_root / "designs" / "fixture-design.md"
    source.parent.mkdir(parents=True)
    source_payload = SOURCE_TEXT.encode("utf-8")
    source.write_bytes(source_payload)
    revision = "b" * 40
    license_payload = (
        b"MIT License\n\nCopyright fixture\n\n"
        b"Permission is hereby granted, free of charge, to any person obtaining a copy.\n"
    )
    license_path = import_root / "LICENSE"
    license_path.write_bytes(license_payload)

    entry: dict[str, Any] = {
        "name": "Fixture Design",
        "author": "Fixture Author",
        "tags": ["Technical", " Dark ", ""],
        "slug": "fixture-design",
        "source_path": "designs/fixture-design.md",
        "sha256": _sha256(source_payload),
    }
    manifest: dict[str, Any] = {
        "upstream": "https://designdotmd.example",
        "upstream_revision": revision,
        "fetched_on": "2026-07-11",
        "license": "MIT",
        "license_url": f"https://designdotmd.example/{revision}/LICENSE",
        "license_evidence_sha256": _sha256(license_payload),
        "entries": [entry],
    }
    manifest_path = import_root / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setattr(importer, "IMPORT_ROOT", import_root)
    monkeypatch.setattr(importer, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(source_registry, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        importer,
        "DESIGNDOTMD_SOURCE",
        ExternalSourceRecord(
            name="designdotmd",
            url="https://designdotmd.example",
            revision=revision,
            license="MIT",
            license_url=f"https://designdotmd.example/{revision}/LICENSE",
            license_evidence_sha256=_sha256(license_payload),
            license_evidence_path="imported-skills/designdotmd/LICENSE",
            manifest_sha256=canonical_ingestion_manifest_sha256(manifest),
            redistribution_obligations=(
                "retain-copyright-notice",
                "retain-license-notice",
            ),
            notice_reference=f"https://designdotmd.example/{revision}/LICENSE",
            source_kind="design-catalog",
            import_mode="metadata-only",
            permission_status="license",
        ),
    )
    return DesignImportFixture(
        root=tmp_path,
        import_root=import_root,
        manifest_path=manifest_path,
        source=source,
        target=tmp_path / "skills",
        manifest=manifest,
        entry=entry,
    )


def _write_source_bytes(design_import: DesignImportFixture, payload: bytes) -> None:
    design_import.source.write_bytes(payload)
    design_import.entry["sha256"] = _sha256(payload)
    _write_bound_manifest(design_import)


def _write_bound_manifest(design_import: DesignImportFixture) -> None:
    design_import.manifest_path.write_text(
        json.dumps(design_import.manifest),
        encoding="utf-8",
    )
    importer.DESIGNDOTMD_SOURCE = replace(
        importer.DESIGNDOTMD_SOURCE,
        manifest_sha256=canonical_ingestion_manifest_sha256(design_import.manifest),
    )


def _write_source(design_import: DesignImportFixture, text: str) -> None:
    _write_source_bytes(design_import, text.encode("utf-8"))


def _run_main(monkeypatch: pytest.MonkeyPatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["import_designdotmd_skills.py", *args])
    importer.main()


def _file_snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def _hardlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.link(target, link)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hard links unavailable: {exc}")


def test_dry_run_reports_pending_install_without_writing(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _file_snapshot(design_import.root)

    _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert _file_snapshot(design_import.root) == before
    assert not design_import.target.exists()
    output = capsys.readouterr().out
    assert "[NEW]" in output
    assert "Mode: dry-run" in output
    assert "Entries: 1  new/updated: 1  unchanged: 0" in output


def test_dry_run_preserves_an_existing_skill(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    skill = design_import.target / "designdotmd-fixture-design" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("existing local content\n", encoding="utf-8")
    before = _file_snapshot(design_import.root)

    _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert _file_snapshot(design_import.root) == before
    assert skill.read_text(encoding="utf-8") == "existing local content\n"
    assert "[UPD]" in capsys.readouterr().out


def test_install_writes_attribution_and_normalized_tags(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    skill = design_import.target / "designdotmd-fixture-design" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")
    assert content.startswith(
        "<!-- designdotmd-import: upstream=https://designdotmd.example "
        "id=fixture-design fetched=2026-07-11 author=Fixture Author -->\n"
    )
    assert 'description: A fixture design system\ntags: ["technical", "dark"]\n' in content
    assert "source: designdotmd\n" in content
    assert "source_url: https://designdotmd.example\n" in content
    assert f"source_revision: {'b' * 40}\n" in content
    assert "license: MIT\n" in content
    assert f"license_url: https://designdotmd.example/{'b' * 40}/LICENSE\n" in content
    assert "import_mode: full-body\n" in content
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert frontmatter["permission_status"] == "license"
    assert frontmatter["permission_reference"] is None
    assert frontmatter["permission_evidence_sha256"] is None
    assert (
        frontmatter["license_evidence_sha256"] == design_import.manifest["license_evidence_sha256"]
    )
    assert frontmatter["license_evidence_path"] == "imported-skills/designdotmd/LICENSE"
    assert frontmatter["manifest_sha256"] == canonical_ingestion_manifest_sha256(
        design_import.manifest
    )
    assert frontmatter["redistribution_obligations"] == [
        "retain-copyright-notice",
        "retain-license-notice",
    ]
    assert frontmatter["notice_reference"] == f"https://designdotmd.example/{'b' * 40}/LICENSE"
    assert frontmatter["source_sha256"] == design_import.entry["sha256"]
    assert "  tags: nested-metadata" in content
    assert content.endswith("# Fixture Design\n\nKeep this body unchanged.\n")
    output = capsys.readouterr().out
    assert "[NEW]" in output
    assert "Mode: install" in output
    assert "Entries: 1  new/updated: 1  unchanged: 0" in output
    assert "Next steps:" in output


def test_unknown_license_blocks_full_body_before_writes(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    design_import.manifest["license"] = "unknown"
    design_import.manifest["license_url"] = None
    design_import.manifest["license_evidence_sha256"] = None
    design_import.manifest_path.write_text(
        json.dumps(design_import.manifest),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        importer,
        "DESIGNDOTMD_SOURCE",
        ExternalSourceRecord(
            name="designdotmd",
            url="https://designdotmd.example",
            revision="b" * 40,
            license="unknown",
            source_kind="design-catalog",
            import_mode="metadata-only",
            permission_status="unknown",
            manifest_sha256=canonical_ingestion_manifest_sha256(design_import.manifest),
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    assert "full-body import blocked by unknown license" in capsys.readouterr().err
    assert not design_import.target.exists()


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "g" * 64])
def test_full_body_entry_requires_a_valid_sha256(
    design_import: DesignImportFixture,
    digest: str | None,
) -> None:
    if digest is None:
        design_import.entry.pop("sha256")
    else:
        design_import.entry["sha256"] = digest

    with pytest.raises(ValueError, match="sha256"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_full_body_source_bytes_must_match_manifest_sha256(
    design_import: DesignImportFixture,
) -> None:
    design_import.source.write_bytes(b"tampered before transformation\n")

    with pytest.raises(ValueError, match="sha256 mismatch") as exc_info:
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert f"manifest={design_import.entry['sha256']}" in str(exc_info.value)
    assert not design_import.target.exists()


def test_manifest_body_and_entry_digest_rewrite_cannot_self_authorize(
    design_import: DesignImportFixture,
) -> None:
    payload = SOURCE_TEXT.replace(
        "Keep this body unchanged.",
        "Unregistered replacement body.",
    ).encode("utf-8")
    design_import.source.write_bytes(payload)
    design_import.entry["sha256"] = _sha256(payload)

    with pytest.raises(ValueError, match="full-manifest binding mismatch"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("upstream", "manifest upstream does not match"),
        ("upstream_revision", "manifest revision does not match"),
        ("fetched_on", "full-manifest binding mismatch"),
        ("name", "full-manifest binding mismatch"),
        ("author", "full-manifest binding mismatch"),
        ("tags", "full-manifest binding mismatch"),
        ("slug", "full-manifest binding mismatch"),
        ("source_path", "full-manifest binding mismatch"),
        ("sha256", "full-manifest binding mismatch"),
        ("extension", "full-manifest binding mismatch"),
    ],
)
def test_registered_full_manifest_rejects_any_trusted_field_mutation(
    design_import: DesignImportFixture,
    field: str,
    message: str,
) -> None:
    mutations: dict[str, object] = {
        "upstream": "https://changed.example/source",
        "upstream_revision": "c" * 40,
        "name": "Changed Name",
        "author": "Changed Author",
        "tags": ["changed"],
        "slug": "changed-slug",
        "source_path": "designs/changed-source.md",
        "sha256": "e" * 64,
    }
    if field in {"upstream", "upstream_revision"}:
        design_import.manifest[field] = mutations[field]
    elif field == "fetched_on":
        design_import.manifest["fetched_on"] = "2026-07-12"
    elif field == "extension":
        design_import.manifest["namespace"] = "changed-extension"
    else:
        design_import.entry[field] = mutations[field]

    with pytest.raises(ValueError, match=message):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_future_permitted_manifest_binds_raw_bytes_into_entity_provenance(
    design_import: DesignImportFixture,
) -> None:
    payload = SOURCE_TEXT.replace(
        "Keep this body unchanged.",
        "Digest-bound body.",
    ).encode("utf-8")
    _write_source_bytes(design_import, payload)

    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    frontmatter = yaml.safe_load(skill.read_text(encoding="utf-8").split("---", 2)[1])
    assert frontmatter["source_sha256"] == _sha256(payload)
    assert "Digest-bound body." in skill.read_text(encoding="utf-8")


def test_entry_outside_registered_manifest_is_rejected_before_source_read(
    design_import: DesignImportFixture,
) -> None:
    unregistered = {
        **design_import.entry,
        "slug": "unregistered",
        "source_path": "designs/unregistered.md",
    }

    with pytest.raises(ValueError, match="not part of the registered full manifest"):
        importer.deploy_entry(
            unregistered,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_duplicate_upstream_provenance_fields_are_rejected(
    design_import: DesignImportFixture,
) -> None:
    _write_source(
        design_import,
        SOURCE_TEXT.replace(
            "name: Fixture Design\n",
            "name: Fixture Design\nsource: attacker\nsource: designdotmd\n",
        ),
    )

    with pytest.raises(ValueError, match="duplicates 'source' provenance"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_install_json_quotes_tags_after_a_block_scalar_description(
    design_import: DesignImportFixture,
) -> None:
    _write_source(
        design_import,
        "---\n"
        "name: Fixture Design\n"
        "description: |-\n"
        "  First description line.\n"
        "  Second description line.\n"
        "typography:\n"
        "  family: Fixture Sans\n"
        "---\n"
        "Body\n",
    )
    design_import.entry["tags"] = ["UI, UX", 'Quote "Tag"', "Hash # Tag", "Bracket ]"]
    _write_bound_manifest(design_import)

    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    content = skill.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert isinstance(frontmatter, dict)
    assert frontmatter["description"] == "First description line.\nSecond description line."
    assert frontmatter["tags"] == ["ui, ux", 'quote "tag"', "hash # tag", "bracket ]"]
    assert (
        "description: |-\n"
        "  First description line.\n"
        "  Second description line.\n"
        'tags: ["ui, ux", "quote \\"tag\\"", "hash # tag", "bracket ]"]\n'
        "typography:\n"
    ) in content


def test_install_places_tags_after_an_indentless_sequence_description(
    design_import: DesignImportFixture,
) -> None:
    _write_source(
        design_import,
        "---\n"
        "name: Fixture Design\n"
        "description:\n"
        "- First description item\n"
        "- Second description item\n"
        "palette: [red, blue]\n"
        "---\n"
        "Body\n",
    )

    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    content = skill.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert isinstance(frontmatter, dict)
    assert frontmatter["description"] == [
        "First description item",
        "Second description item",
    ]
    assert frontmatter["tags"] == ["technical", "dark"]
    assert (
        "description:\n"
        "- First description item\n"
        "- Second description item\n"
        'tags: ["technical", "dark"]\n'
        "palette: [red, blue]\n"
    ) in content


@pytest.mark.parametrize(
    ("description_yaml", "expected_description"),
    [
        (
            "description: Visible value # note: valid inline comment\n",
            "Visible value",
        ),
        ('description: "Quoted: description"\n', "Quoted: description"),
        (
            "description: |-\n  Block: description\n  second line\n",
            "Block: description\nsecond line",
        ),
        (
            "description: Plain description\n  continued across lines\n",
            "Plain description continued across lines",
        ),
    ],
)
def test_valid_description_forms_are_preserved_before_tag_injection(
    design_import: DesignImportFixture,
    description_yaml: str,
    expected_description: str,
) -> None:
    _write_source(
        design_import,
        f"---\nname: Fixture Design\n{description_yaml}palette: [red, blue]\n---\nBody\n",
    )

    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    content = skill.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert isinstance(frontmatter, dict)
    assert frontmatter["description"] == expected_description
    assert description_yaml in content


def test_description_repair_requires_parser_evidence_at_the_value_colon(
    design_import: DesignImportFixture,
) -> None:
    source_text = (
        "---\n"
        "name: [broken\n"
        "description: Repairable-looking value: must remain untouched\n"
        "---\n"
        "Body\n"
    )
    _write_source(design_import, source_text)

    assert importer._quote_invalid_description_scalar(source_text) == source_text
    with pytest.raises(ValueError, match="invalid YAML frontmatter"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_yaml_and_target_guards_reject_edge_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert importer._inject_tags(SOURCE_TEXT, []) == SOURCE_TEXT
    assert importer._inject_tags("Body without frontmatter\n", ["tag"]) == (
        "Body without frontmatter\n"
    )
    sequence_frontmatter = "---\n- item\n---\nBody\n"
    assert importer._inject_tags(sequence_frontmatter, ["tag"]) == sequence_frontmatter

    with pytest.raises(ValueError, match="frontmatter must be a mapping"):
        importer._validate_frontmatter(sequence_frontmatter, source=tmp_path / "sequence.md")
    with pytest.raises(ValueError, match="expected non-empty string"):
        importer._validate("slug", "")

    target = tmp_path / "target"
    target.mkdir()
    with pytest.raises(ValueError, match="resolves outside target_dir"):
        importer._require_target_containment(
            tmp_path / "outside" / "SKILL.md",
            target,
            label="SKILL.md",
        )

    def fail_resolve(self: Path, strict: bool = False) -> Path:
        raise RuntimeError("symlink loop")

    monkeypatch.setattr(Path, "resolve", fail_resolve)
    with pytest.raises(ValueError, match="could not be resolved: symlink loop"):
        importer._resolve_target_root(target)


def test_install_quotes_the_corpus_plain_description_shape(
    design_import: DesignImportFixture,
) -> None:
    _write_source(
        design_import,
        "---\n"
        "name: 3D Sculpt\n"
        "description: 3D viewport: studio grey, mesh cyan, normal magenta.\n"
        "---\n"
        "Body\n",
    )

    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    content = skill.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert isinstance(frontmatter, dict)
    assert frontmatter["description"] == "3D viewport: studio grey, mesh cyan, normal magenta."
    assert (
        'description: "3D viewport: studio grey, mesh cyan, normal magenta."\n'
        'tags: ["technical", "dark"]\n'
    ) in content


def test_checked_in_unknown_license_corpus_fails_closed_without_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = importer.load_manifest()
    entries = manifest["entries"]
    assert entries

    for entry in entries:
        source = importer.IMPORT_ROOT / entry["source_path"]
        match = importer._FM_OPEN_RE.match(source.read_text(encoding="utf-8"))
        assert match is not None
        try:
            yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            assert mark is not None
            failed_line = match.group(1).splitlines()[mark.line]
            assert failed_line.startswith("description:")
            assert ": " in failed_line.removeprefix("description:")

    target = tmp_path / "real-corpus-dry-run"
    with pytest.raises(SystemExit) as exc_info:
        importer._preflight_manifest(manifest, target)

    assert exc_info.value.code == 1
    assert "registered immutable full-manifest binding" in capsys.readouterr().err
    assert not target.exists()


def test_invalid_rendered_frontmatter_names_source_and_location(
    design_import: DesignImportFixture,
) -> None:
    _write_source(
        design_import,
        "---\n"
        "name: Fixture Design\n"
        "description: Broken fixture: known upstream shape\n"
        "palette: [red\n"
        "---\n"
        "Body\n",
    )

    with pytest.raises(ValueError) as exc_info:
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    message = str(exc_info.value)
    assert str(design_import.source) in message
    assert "invalid YAML frontmatter" in message
    assert "line 5, column 1" in message
    assert not design_import.target.exists()


@pytest.mark.parametrize("closing_fence", ["----", "---suffix", "--- # comment", "--- "])
def test_frontmatter_closing_fence_must_be_exact(
    design_import: DesignImportFixture,
    closing_fence: str,
) -> None:
    _write_source(
        design_import,
        f"---\nname: Fixture Design\ndescription: Valid description\n{closing_fence}\nBody\n",
    )

    with pytest.raises(ValueError, match="missing YAML frontmatter delimiters"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative replacement"
)
def test_reinstall_is_idempotent_and_does_not_rewrite(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill, changed = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )
    original = skill.read_bytes()
    assert changed is True

    def fail_write(*_args: object, **_kwargs: object) -> None:
        pytest.fail("idempotent deployment attempted to rewrite SKILL.md")

    monkeypatch.setattr(importer, "_atomic_write_text", fail_write)
    same_skill, changed = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    assert same_skill == skill
    assert changed is False
    assert skill.read_bytes() == original


def test_existing_attribution_and_top_level_tags_are_not_duplicated(
    design_import: DesignImportFixture,
) -> None:
    _write_source(
        design_import,
        "<!-- designdotmd-import: upstream=old id=old fetched=old author=old -->\n"
        "---\n"
        "name: Fixture Design\n"
        "description: Existing metadata\n"
        "tags: [upstream]\n"
        "---\n"
        "Body\n",
    )

    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    content = skill.read_text(encoding="utf-8")
    assert content.count("<!-- designdotmd-import:") == 1
    assert "upstream=old" not in content
    assert content.count("\ntags:") == 1
    assert "tags: [upstream]" in content
    assert "tags: [technical, dark]" not in content


@pytest.mark.parametrize(
    "slug",
    ["../escape", "nested/escape", "Uppercase", "a" * 65, "line-break\n"],
)
def test_manifest_slug_must_match_strict_contained_format(
    design_import: DesignImportFixture,
    slug: str,
) -> None:
    design_import.entry["slug"] = slug

    with pytest.raises(
        ValueError,
        match=r"manifest\.entries\[1\]\.slug: expected a canonical",
    ):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=True,
        )

    assert not design_import.target.exists()


@pytest.mark.parametrize("source_path", ["../outside.md", "designs/../../outside.md"])
def test_manifest_source_traversal_is_rejected(
    design_import: DesignImportFixture,
    source_path: str,
) -> None:
    design_import.entry["source_path"] = source_path

    with pytest.raises(
        ValueError,
        match="canonical repository-relative POSIX path",
    ):
        _write_bound_manifest(design_import)

    assert not design_import.target.exists()


def test_missing_manifest_source_error_names_the_resolved_path(
    design_import: DesignImportFixture,
) -> None:
    design_import.entry["source_path"] = "designs/missing.md"
    _write_bound_manifest(design_import)
    expected = (design_import.import_root / "designs" / "missing.md").resolve()

    with pytest.raises(FileNotFoundError) as exc_info:
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert str(exc_info.value) == f"Source design missing: {expected}"
    assert not design_import.target.exists()


def test_manifest_source_symlink_escape_is_rejected(
    design_import: DesignImportFixture,
) -> None:
    outside = design_import.root / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = design_import.source.parent / "linked.md"
    _symlink_or_skip(link, outside)
    design_import.entry["source_path"] = "designs/linked.md"
    _write_bound_manifest(design_import)

    with pytest.raises(ValueError, match=r"source_path: .* resolves outside import root"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative source reads"
)
def test_source_parent_swap_after_resolution_is_rejected(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = design_import.root / "outside-source"
    outside.mkdir()
    (outside / design_import.source.name).write_bytes(
        (SOURCE_TEXT + "malicious\n").encode("utf-8"),
    )
    source_parent = design_import.source.parent
    displaced = design_import.import_root / "displaced-designs"
    original_resolve = importer._resolve_within
    swapped = False

    def resolve_then_swap(root: Path, candidate_rel: str, *, field: str) -> Path:
        nonlocal swapped
        resolved = original_resolve(root, candidate_rel, field=field)
        if field == "source_path" and not swapped:
            source_parent.rename(displaced)
            _symlink_or_skip(source_parent, outside, target_is_directory=True)
            swapped = True
        return resolved

    monkeypatch.setattr(importer, "_resolve_within", resolve_then_swap)

    with pytest.raises(ValueError, match="source path changed or is not a real file"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert swapped is True
    assert not design_import.target.exists()


def test_destination_symlink_escape_is_rejected(
    design_import: DesignImportFixture,
) -> None:
    outside = design_import.root / "outside-target"
    outside.mkdir()
    design_import.target.mkdir()
    _symlink_or_skip(
        design_import.target / "designdotmd-fixture-design",
        outside,
        target_is_directory=True,
    )

    with pytest.raises(
        ValueError,
        match=(
            r"skill dir .* resolves (?:outside target_dir|"
            r"through a reparse point beneath target_dir)"
        ),
    ):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not (outside / "SKILL.md").exists()


def test_target_must_be_a_real_directory(
    design_import: DesignImportFixture,
) -> None:
    design_import.target.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"target directory .* must be a real directory"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=True,
        )

    assert design_import.target.read_text(encoding="utf-8") == "not a directory\n"


@pytest.mark.parametrize("outside_exists", [False, True])
def test_final_skill_symlink_escape_is_rejected_without_following_it(
    design_import: DesignImportFixture,
    outside_exists: bool,
) -> None:
    outside_dir = design_import.root / "outside-target"
    outside_dir.mkdir()
    outside_skill = outside_dir / "SKILL.md"
    if outside_exists:
        outside_skill.write_text("outside content\n", encoding="utf-8")

    skill_dir = design_import.target / "designdotmd-fixture-design"
    skill_dir.mkdir(parents=True)
    _symlink_or_skip(skill_dir / "SKILL.md", outside_skill)

    with pytest.raises(
        ValueError,
        match=(
            r"SKILL\.md .* resolves (?:outside target_dir|"
            r"through a reparse point beneath target_dir)"
        ),
    ):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    if outside_exists:
        assert outside_skill.read_text(encoding="utf-8") == "outside content\n"
    else:
        assert not outside_skill.exists()


def test_install_atomically_replaces_a_hard_link_without_changing_outside_inode(
    design_import: DesignImportFixture,
) -> None:
    skill = design_import.target / "designdotmd-fixture-design" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    outside = design_import.root / "outside-hard-link.md"
    outside.write_text("outside content\n", encoding="utf-8")
    _hardlink_or_skip(skill, outside)
    assert os.path.samefile(skill, outside)

    deployed, changed = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    assert deployed == skill
    assert changed is True
    assert outside.read_text(encoding="utf-8") == "outside content\n"
    assert not os.path.samefile(skill, outside)
    assert "# Fixture Design" in skill.read_text(encoding="utf-8")
    assert not list(skill.parent.glob(".SKILL.md.*"))


def test_install_detaches_an_unchanged_hard_link(
    design_import: DesignImportFixture,
) -> None:
    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )
    expected = skill.read_text(encoding="utf-8")
    skill.unlink()
    outside = design_import.root / "outside-unchanged-hard-link.md"
    outside.write_text(expected, encoding="utf-8")
    _hardlink_or_skip(skill, outside)

    _, changed = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    assert changed is True
    assert not os.path.samefile(skill, outside)
    assert outside.read_text(encoding="utf-8") == expected
    assert skill.read_text(encoding="utf-8") == expected


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative replacement"
)
def test_install_replacement_stays_in_open_parent_during_directory_swap(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = design_import.target / "designdotmd-fixture-design"
    skill_dir.mkdir(parents=True)
    skill = skill_dir / "SKILL.md"
    skill.write_text("stale local content\n", encoding="utf-8")

    outside_dir = design_import.root / "outside-target"
    outside_dir.mkdir()
    outside_skill = outside_dir / "SKILL.md"
    outside_skill.write_text("outside content\n", encoding="utf-8")
    displaced_dir = design_import.target / "displaced-fixture-design"
    original_replace = os.replace
    swapped = False

    def swap_then_replace(
        src: Any,
        dst: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        assert src_dir_fd is not None
        assert dst_dir_fd == src_dir_fd
        if not swapped:
            skill_dir.rename(displaced_dir)
            _symlink_or_skip(skill_dir, outside_dir, target_is_directory=True)
            swapped = True
        original_replace(
            src,
            dst,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(importer.os, "replace", swap_then_replace)

    deployed, changed = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    assert deployed == skill
    assert changed is True
    assert swapped is True
    assert outside_skill.read_text(encoding="utf-8") == "outside content\n"
    assert "# Fixture Design" in (displaced_dir / "SKILL.md").read_text(encoding="utf-8")
    assert not list(displaced_dir.glob(".SKILL.md.*"))


@pytest.mark.skipif(
    not importer._supports_directory_fds(), reason="requires descriptor-relative replacement"
)
def test_descriptor_replace_failure_removes_staged_file(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill = design_import.target / "designdotmd-fixture-design" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("stale local content\n", encoding="utf-8")
    original_replace = os.replace

    def fail_descriptor_replace(
        source: Any,
        destination: Any,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if src_dir_fd is not None:
            raise OSError("replace failed")
        original_replace(source, destination)

    monkeypatch.setattr(importer.os, "replace", fail_descriptor_replace)

    with pytest.raises(OSError, match="replace failed"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert skill.read_text(encoding="utf-8") == "stale local content\n"
    assert not list(skill.parent.glob(".SKILL.md.*"))


def test_install_fails_closed_without_safe_filesystem_primitives(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importer, "_supports_directory_fds", lambda: False)

    with pytest.raises(RuntimeError, match="secure source read unavailable"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_source_directory_is_rejected_as_non_regular(
    design_import: DesignImportFixture,
) -> None:
    design_import.entry["source_path"] = "designs"
    _write_bound_manifest(design_import)

    with pytest.raises(ValueError, match="source is not a regular file"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )


@pytest.mark.parametrize("alias_kind", ["ancestor", "target-root"])
def test_cli_installs_through_a_trusted_symlink_target(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    alias_kind: str,
) -> None:
    if alias_kind == "ancestor":
        real_parent = design_import.root / "real-parent"
        real_parent.mkdir()
        alias_parent = design_import.root / "alias-parent"
        _symlink_or_skip(alias_parent, real_parent, target_is_directory=True)
        selected_target = alias_parent / "skills"
    else:
        real_target = design_import.root / "real-target"
        real_target.mkdir()
        selected_target = design_import.root / "skills-link"
        _symlink_or_skip(selected_target, real_target, target_is_directory=True)

    resolved_target = selected_target.resolve()
    _run_main(monkeypatch, "--install", "--target", str(selected_target))

    installed = resolved_target / "designdotmd-fixture-design" / "SKILL.md"
    assert installed.is_file()
    assert (selected_target / "designdotmd-fixture-design" / "SKILL.md").samefile(installed)


def test_symlink_beneath_resolved_target_is_rejected(
    design_import: DesignImportFixture,
) -> None:
    design_import.target.mkdir()
    real_skill_dir = design_import.target / "real-skill-dir"
    real_skill_dir.mkdir()
    _symlink_or_skip(
        design_import.target / "designdotmd-fixture-design",
        real_skill_dir,
        target_is_directory=True,
    )

    with pytest.raises(ValueError, match=r"(?:symlink|reparse point) beneath target_dir"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not (real_skill_dir / "SKILL.md").exists()


def test_atomic_replacement_preserves_existing_destination_mode(
    design_import: DesignImportFixture,
) -> None:
    skill, _ = importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )
    skill.chmod(0o751)
    _write_source(
        design_import,
        SOURCE_TEXT.replace("Keep this body unchanged.", "Updated while preserving mode."),
    )

    importer.deploy_entry(
        design_import.entry,
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )

    assert stat.S_IMODE(skill.stat().st_mode) == 0o751


def test_new_destination_mode_respects_process_umask(
    design_import: DesignImportFixture,
) -> None:
    previous_umask = os.umask(0o027)
    try:
        skill, _ = importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(skill.stat().st_mode) == 0o640


def test_manifest_tags_type_error_names_entry_and_received_type(
    design_import: DesignImportFixture,
) -> None:
    design_import.entry["tags"] = "technical"

    with pytest.raises(
        ValueError,
        match=r"manifest\.entries\[1\]\.tags: expected a list of strings",
    ):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_cli_requires_an_explicit_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch)

    assert exc_info.value.code == 2
    stderr = capsys.readouterr().err
    assert "usage:" in stderr
    assert "Pass either --install or --dry-run" in stderr


def test_cli_rejects_conflicting_modes_without_writing(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run_main(
            monkeypatch,
            "--install",
            "--dry-run",
            "--target",
            str(design_import.target),
        )

    assert exc_info.value.code == 2
    assert "--install and --dry-run cannot be combined" in capsys.readouterr().err
    assert not design_import.target.exists()


def test_missing_manifest_error_explains_how_to_rebuild(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    design_import.manifest_path.unlink()

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert f"Manifest not found: {design_import.manifest_path}" in stderr
    assert "Run: python imported-skills/designdotmd/build_manifest.py" in stderr
    assert not design_import.target.exists()


def test_malformed_manifest_json_is_a_concise_exit_1_error(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    design_import.manifest_path.write_text('{"entries": [}\n', encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert f"Malformed JSON in manifest {design_import.manifest_path}" in stderr
    assert "line 1, column 14" in stderr
    assert "Traceback" not in stderr
    assert not design_import.target.exists()


def test_malformed_attribution_is_a_concise_exit_1_error(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_source(
        design_import,
        "<!-- designdotmd-import: upstream=broken\n" + SOURCE_TEXT,
    )

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "entry 1 ('fixture-design'):" in stderr
    assert "malformed designdotmd attribution comment" in stderr
    assert "Traceback" not in stderr
    assert not design_import.target.exists()


@pytest.mark.parametrize("blocked_file", ["manifest", "source"])
def test_cli_read_permission_failure_is_concise(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blocked_file: str,
) -> None:
    blocked_path = (
        design_import.manifest_path if blocked_file == "manifest" else design_import.source
    )
    if blocked_file == "manifest":
        original_read_text = Path.read_text

        def denied_read_text(
            self: Path,
            encoding: str | None = None,
            errors: str | None = None,
        ) -> str:
            if self == blocked_path:
                raise PermissionError("permission denied")
            return original_read_text(self, encoding=encoding, errors=errors)

        monkeypatch.setattr(Path, "read_text", denied_read_text)
    else:
        original_open = os.open
        supports_directory_fds = importer._supports_directory_fds()

        def denied_open(
            path: Any,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if (path == design_import.source.name and dir_fd is not None) or (
                path == design_import.source and dir_fd is None
            ):
                raise PermissionError("permission denied")
            if dir_fd is None:
                return original_open(path, flags, mode)
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(importer.os, "open", denied_open)
        if supports_directory_fds:
            monkeypatch.setattr(importer, "_supports_directory_fds", lambda: True)

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    expected_context = "could not read manifest" if blocked_file == "manifest" else "entry 1"
    assert expected_context in stderr
    assert "permission denied" in stderr
    assert "Traceback" not in stderr
    assert not design_import.target.exists()


@pytest.mark.parametrize("invalid_file", ["manifest", "source"])
def test_cli_invalid_utf8_failure_is_concise(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    invalid_file: str,
) -> None:
    if invalid_file == "manifest":
        design_import.manifest_path.write_bytes(b"\xff")
    else:
        _write_source_bytes(design_import, b"\xff")

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    expected_context = "manifest" if invalid_file == "manifest" else "entry 1"
    assert expected_context in stderr
    assert "not valid UTF-8" in stderr
    assert "Traceback" not in stderr
    assert not design_import.target.exists()


def test_cli_entry_validation_failure_is_a_concise_exit_1_error(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    design_import.entry["tags"] = "technical"
    design_import.manifest_path.write_text(json.dumps(design_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert (
        "entry 1 ('fixture-design'): manifest.entries[1].tags: expected a list of strings"
    ) in stderr
    assert "Traceback" not in stderr
    assert not design_import.target.exists()


def test_unsafe_manifest_upstream_is_rejected_before_attribution(
    design_import: DesignImportFixture,
) -> None:
    design_import.manifest["upstream"] = "https://example.test -->\ninjected"

    with pytest.raises(ValueError, match="upstream.*whitespace or control"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


def test_registered_unsafe_author_is_rejected_during_attribution(
    design_import: DesignImportFixture,
) -> None:
    design_import.entry["author"] = "Unsafe --> author"
    _write_bound_manifest(design_import)

    with pytest.raises(ValueError, match="author.*unsafe attribution value"):
        importer.deploy_entry(
            design_import.entry,
            design_import.manifest,
            design_import.target,
            dry_run=False,
        )

    assert not design_import.target.exists()


@pytest.mark.parametrize(
    ("entries", "expected_error"),
    [(None, "manifest entries must be a list"), ([None], "expected an object")],
)
def test_manifest_entries_shape_is_rejected_before_writes(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entries: object,
    expected_error: str,
) -> None:
    design_import.manifest["entries"] = entries
    design_import.manifest_path.write_text(json.dumps(design_import.manifest), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    assert expected_error in capsys.readouterr().err
    assert not design_import.target.exists()


def test_install_preflights_malformed_later_entry_before_creating_target(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    malformed_source = design_import.source.parent / "broken-design.md"
    malformed_payload = (
        "---\nname: Broken Design\ndescription: Broken\npalette: [red\n---\nBody\n"
    ).encode("utf-8")
    malformed_source.write_bytes(malformed_payload)
    malformed_entry = {
        **design_import.entry,
        "name": "Broken Design",
        "slug": "broken-design",
        "source_path": "designs/broken-design.md",
        "sha256": _sha256(malformed_payload),
    }
    design_import.manifest["entries"].append(malformed_entry)
    _write_bound_manifest(design_import)

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "entry 2 ('broken-design'):" in stderr
    assert "invalid YAML frontmatter" in stderr
    assert "Traceback" not in stderr
    assert not design_import.target.exists()


def test_install_preflights_each_destination_parent_before_any_commit(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    second_source = design_import.source.parent / "blocked-design.md"
    second_source.write_bytes(SOURCE_TEXT.encode("utf-8"))
    second_entry = {
        **design_import.entry,
        "name": "Blocked Design",
        "slug": "blocked-design",
        "source_path": "designs/blocked-design.md",
    }
    design_import.manifest["entries"].append(second_entry)
    _write_bound_manifest(design_import)

    design_import.target.mkdir()
    blocked_parent = design_import.target / "designdotmd-blocked-design"
    blocked_parent.write_text("not a directory\n", encoding="utf-8")
    first_skill = design_import.target / "designdotmd-fixture-design" / "SKILL.md"

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "entry 2 ('blocked-design'):" in stderr
    assert "destination parent" in stderr
    assert "must be a real directory" in stderr
    assert "Traceback" not in stderr
    assert not first_skill.exists()
    assert blocked_parent.read_text(encoding="utf-8") == "not a directory\n"


@pytest.mark.parametrize("blocker_kind", ["directory", "fifo"])
def test_install_preflights_each_final_destination_before_any_commit(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blocker_kind: str,
) -> None:
    second_source = design_import.source.parent / "blocked-design.md"
    second_source.write_bytes(SOURCE_TEXT.encode("utf-8"))
    second_entry = {
        **design_import.entry,
        "name": "Blocked Design",
        "slug": "blocked-design",
        "source_path": "designs/blocked-design.md",
    }
    design_import.manifest["entries"].append(second_entry)
    _write_bound_manifest(design_import)

    blocked_destination = design_import.target / "designdotmd-blocked-design" / "SKILL.md"
    blocked_destination.parent.mkdir(parents=True)
    if blocker_kind == "directory":
        blocked_destination.mkdir()
    else:
        mkfifo = getattr(os, "mkfifo", None)
        if mkfifo is None:
            pytest.skip("FIFOs unavailable on this platform")
        mkfifo(blocked_destination)
    first_skill = design_import.target / "designdotmd-fixture-design" / "SKILL.md"

    with pytest.raises(SystemExit) as exc_info:
        _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "entry 2 ('blocked-design'):" in stderr
    assert "must be a regular file" in stderr
    assert "Traceback" not in stderr
    assert not first_skill.exists()
    if blocker_kind == "directory":
        assert blocked_destination.is_dir()
    else:
        assert stat.S_ISFIFO(blocked_destination.lstat().st_mode)


def test_full_manifest_binding_rejects_duplicate_destination_slugs_before_writes(
    design_import: DesignImportFixture,
) -> None:
    duplicate_source = design_import.import_root / "designs" / "duplicate-source.md"
    duplicate_source.write_bytes(design_import.source.read_bytes())
    duplicate_entry = dict(design_import.entry)
    duplicate_entry["source_path"] = "designs/duplicate-source.md"
    design_import.manifest["entries"].append(duplicate_entry)

    with pytest.raises(ValueError, match="duplicate ingestion slug"):
        _write_bound_manifest(design_import)

    assert not design_import.target.exists()


def test_mixed_status_output_is_truthful_and_elided_once(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries: list[dict[str, Any]] = []
    for number in range(1, 8):
        slug = f"fixture-design-{number}"
        source = design_import.source.parent / f"{slug}.md"
        source.write_bytes(SOURCE_TEXT.encode("utf-8"))
        entries.append(
            {
                **design_import.entry,
                "name": f"Fixture Design {number}",
                "slug": slug,
                "source_path": f"designs/{slug}.md",
            }
        )
    design_import.manifest["entries"] = entries
    _write_bound_manifest(design_import)

    last_skill, _ = importer.deploy_entry(
        entries[-1],
        design_import.manifest,
        design_import.target,
        dry_run=False,
    )
    updated_skill = design_import.target / "designdotmd-fixture-design-2" / "SKILL.md"
    updated_skill.parent.mkdir(parents=True)
    updated_skill.write_text("stale local content\n", encoding="utf-8")

    _run_main(monkeypatch, "--dry-run", "--target", str(design_import.target))

    output = capsys.readouterr().out
    first_skill = design_import.target / "designdotmd-fixture-design-1" / "SKILL.md"
    assert f"[NEW] {first_skill.relative_to(design_import.target.parent)}" in output
    assert f"[UPD] {updated_skill.relative_to(design_import.target.parent)}" in output
    assert f"[   ] {last_skill.relative_to(design_import.target.parent)}" in output
    assert output.count("entries omitted") == 1
    assert "... (1 entries omitted) ..." in output
    assert "Entries: 7  new/updated: 6  unchanged: 1" in output


def test_install_status_uses_actual_guarded_destination_state(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_deploy = importer._deploy_prepared
    injected = False

    def create_destination_before_commit(
        prepared: tuple[Path, Path, str],
        target_dir: Path,
        dry_run: bool,
    ) -> tuple[Path, bool, bool]:
        nonlocal injected
        destination = prepared[1]
        if not injected:
            destination.parent.mkdir(parents=True)
            destination.write_text("stale content\n", encoding="utf-8")
            injected = True
        return original_deploy(prepared, target_dir, dry_run)

    monkeypatch.setattr(importer, "_deploy_prepared", create_destination_before_commit)

    _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    assert injected is True
    assert "[UPD]" in capsys.readouterr().out


def test_install_commits_the_exact_preflighted_source_payload(
    design_import: DesignImportFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_prepare = importer._prepare_entry
    prepare_calls = 0

    def prepare_then_mutate_source(
        entry: dict,
        manifest: dict,
        target_dir: Path,
    ) -> tuple[Path, Path, str]:
        nonlocal prepare_calls
        prepared = original_prepare(entry, manifest, target_dir)
        prepare_calls += 1
        design_import.source.write_text("malformed replacement\n", encoding="utf-8")
        return prepared

    monkeypatch.setattr(importer, "_prepare_entry", prepare_then_mutate_source)

    _run_main(monkeypatch, "--install", "--target", str(design_import.target))

    installed = design_import.target / "designdotmd-fixture-design" / "SKILL.md"
    assert prepare_calls == 1
    assert "# Fixture Design" in installed.read_text(encoding="utf-8")
    assert "malformed replacement" not in installed.read_text(encoding="utf-8")
