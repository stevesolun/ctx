from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ctx.core.source_registry import (
    BUILTIN_EXTERNAL_SOURCES,
    ExternalSourceRecord,
    LicenseGateError,
    canonical_ingestion_manifest_sha256,
    entity_provenance,
    get_external_source,
    load_source_registry,
    render_third_party_notice,
    validate_ingestion_manifest,
    validate_import_plan,
    validate_source_registry,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MATT_REVISION = "e74f0061bb67222181640effa98c675bdb2fdaa7"
MATT_LICENSE_PATH = "imported-skills/mattpocock/LICENSE"
MATT_LICENSE_SHA256 = "0e7ac423bf2c6e223b7c5b156f8cf72da49d748e56a1641402c31f22ad07dbb5"
RETAIN_MIT = ("retain-copyright-notice", "retain-license-notice")


def _manifest_entry(
    *,
    slug: str = "example",
    source_path: str = "skills/example.md",
    sha256: str = "d" * 64,
) -> dict[str, Any]:
    return {
        "name": "Example",
        "author": "Fixture Author",
        "tags": ["fixture"],
        "slug": slug,
        "source_path": source_path,
        "sha256": sha256,
    }


def _ingestion_manifest(
    *,
    revision: str = "b" * 40,
    entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "upstream": "https://example.test/source",
        "upstream_revision": revision,
        "fetched_on": "2026-07-11",
        "license": "MIT",
        "license_url": f"https://example.test/{revision}/LICENSE",
        "license_evidence_sha256": MATT_LICENSE_SHA256,
        "entries": entries if entries is not None else [_manifest_entry()],
    }


def test_license_gate_allows_full_body_for_policy_allowlisted_license() -> None:
    record = ExternalSourceRecord(
        name="optillm",
        url="https://github.com/algorithmicsuperintelligence/optillm",
        revision=MATT_REVISION,
        license="MIT",
        license_url=f"https://example.test/{MATT_REVISION}/LICENSE",
        license_evidence_sha256=MATT_LICENSE_SHA256,
        license_evidence_path=MATT_LICENSE_PATH,
        redistribution_obligations=RETAIN_MIT,
        notice_reference=f"https://example.test/{MATT_REVISION}/LICENSE",
        source_kind="harness",
        import_mode="full",
        permission_status="license",
    )

    assert validate_import_plan(record) == record


def test_license_gate_blocks_noncommercial_full_body_without_permission() -> None:
    record = ExternalSourceRecord(
        name="academic-research-skills",
        url="https://github.com/Imbad0202/academic-research-skills",
        revision="a" * 40,
        license="CC BY-NC 4.0",
        license_url="https://example.test/academic/LICENSE",
        source_kind="skill-suite",
        import_mode="full",
        permission_status="license",
    )

    with pytest.raises(LicenseGateError, match="non-commercial"):
        validate_import_plan(record)


def test_license_gate_allows_metadata_only_for_restricted_license() -> None:
    record = ExternalSourceRecord(
        name="academic-research-skills",
        url="https://github.com/Imbad0202/academic-research-skills",
        revision="153203d",
        license="CC BY-NC 4.0",
        license_url="https://example.test/academic/LICENSE",
        source_kind="skill-suite",
        import_mode="metadata-only",
        permission_status="license",
    )

    assert validate_import_plan(record) == record


def test_license_gate_allows_full_body_when_permission_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    permission_payload = b"Permission granted for redistribution of this fixture.\n"
    permission_path = tmp_path / "imported-skills" / "fixture" / "permission.txt"
    permission_path.parent.mkdir(parents=True)
    permission_path.write_bytes(permission_payload)
    permission_digest = hashlib.sha256(permission_payload).hexdigest()
    permission_reference = "imported-skills/fixture/permission.txt"
    monkeypatch.setattr("ctx.core.source_registry.REPO_ROOT", tmp_path)
    record = ExternalSourceRecord(
        name="academic-research-skills",
        url="https://github.com/Imbad0202/academic-research-skills",
        revision="a" * 40,
        license="unknown",
        source_kind="skill-suite",
        import_mode="full",
        permission_status="explicit-permission",
        permission_reference=permission_reference,
        permission_evidence_sha256=permission_digest,
        redistribution_obligations=("preserve-attribution",),
        notice_reference=permission_reference,
    )

    assert validate_import_plan(record) == record


def test_builtin_registry_records_requested_sources() -> None:
    names = {source.name for source in BUILTIN_EXTERNAL_SOURCES}

    assert {
        "mattpocock-skills",
        "academic-research-skills",
        "agents-md",
        "lat-md",
        "optillm",
        "julius-caveman",
        "strix",
        "no-mistakes",
        "designdotmd",
        "skills-sh",
    } <= names


def test_builtin_registry_records_are_valid_and_full_body_sources_are_pinned() -> None:
    assert validate_source_registry(BUILTIN_EXTERNAL_SOURCES) == BUILTIN_EXTERNAL_SOURCES
    for source in BUILTIN_EXTERNAL_SOURCES:
        if source.import_mode in {"full", "full-body"}:
            assert len(source.revision) == 40
            assert source.license_url
            assert source.license_evidence_path
            assert source.license_evidence_sha256
            assert source.redistribution_obligations
            assert source.notice_reference


def test_full_body_evidence_files_are_checked_in() -> None:
    full_body_sources = [
        source for source in BUILTIN_EXTERNAL_SOURCES if source.import_mode in {"full", "full-body"}
    ]
    evidence_paths = [
        source.license_evidence_path
        for source in full_body_sources
        if source.license_evidence_path is not None
    ]
    assert len(evidence_paths) == len(full_body_sources)

    subprocess.run(
        ["git", "ls-files", "--error-unmatch", *evidence_paths],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def test_license_gate_blocks_unknown_full_body() -> None:
    source = ExternalSourceRecord(
        name="unknown-source",
        url="https://example.test/source",
        revision="a" * 40,
        license="unknown",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="unknown",
    )

    with pytest.raises(LicenseGateError, match="unknown license"):
        validate_import_plan(source)


def test_license_gate_requires_evidence_url_for_known_license() -> None:
    source = ExternalSourceRecord(
        name="missing-evidence",
        url="https://example.test/source",
        revision="a" * 40,
        license="MIT",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="license",
    )

    with pytest.raises(ValueError, match="requires license_url"):
        validate_import_plan(source)


def test_metadata_only_keeps_legacy_registry_compatibility_without_license_url() -> None:
    source = ExternalSourceRecord(
        name="legacy-metadata",
        url="https://example.test/source",
        revision="snapshot",
        license="MIT",
        source_kind="external-catalog",
        import_mode="metadata-only",
        permission_status="license",
    )

    assert validate_import_plan(source) == source


def test_validation_normalizes_urls_modes_status_and_optional_strings() -> None:
    source = ExternalSourceRecord(
        name="normalized",
        url=" HTTPS://Example.TEST:443/source/ ",
        revision=" snapshot ",
        license=" MIT ",
        license_url=" HTTPS://EXAMPLE.TEST/LICENSE/ ",
        source_kind=" catalog ",
        import_mode=" Metadata-Only ",
        permission_status=" LICENSE ",
        permission_reference="   ",
        notes=" review upstream ",
    )

    validated = validate_import_plan(source)

    assert validated.url == "https://example.test/source"
    assert validated.license_url == "https://example.test/LICENSE"
    assert validated.import_mode == "metadata-only"
    assert validated.permission_status == "license"
    assert validated.permission_reference is None
    assert validated.notes == "review upstream"


def test_whitespace_permission_reference_fails_for_explicit_permission() -> None:
    source = ExternalSourceRecord(
        name="missing-permission-evidence",
        url="https://example.test/source",
        revision="snapshot",
        license="unknown",
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status="explicit-permission",
        permission_reference=" \t ",
    )

    with pytest.raises(ValueError, match="requires permission_reference"):
        validate_import_plan(source)


@pytest.mark.parametrize(
    ("license_name", "permission_status", "permission_reference", "message"),
    [
        ("unknown", "license", None, "unknown license cannot use"),
        ("MIT", "unknown", None, "known license cannot use"),
        ("MIT", "license", "author email", "canonical checked-in path"),
    ],
)
def test_license_permission_matrix_rejects_contradictions(
    license_name: str,
    permission_status: str,
    permission_reference: str | None,
    message: str,
) -> None:
    source = ExternalSourceRecord(
        name="contradictory",
        url="https://example.test/source",
        revision="snapshot",
        license=license_name,
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status=permission_status,
        permission_reference=permission_reference,
    )

    with pytest.raises(ValueError, match=message):
        validate_import_plan(source)


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://user@example.test/source",
        "https://example.test/source?download=1",
        "https://example.test/source#license",
        "https://example.test/source%0Ainjected",
    ],
)
def test_source_url_validation_rejects_ambiguous_or_unsafe_https_urls(url: str) -> None:
    source = ExternalSourceRecord(
        name="unsafe-url",
        url=url,
        revision="snapshot",
        license="MIT",
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status="license",
    )

    with pytest.raises(ValueError, match="URL"):
        validate_import_plan(source)


def test_license_url_uses_the_same_strict_https_validation() -> None:
    source = ExternalSourceRecord(
        name="unsafe-license-url",
        url="https://example.test/source",
        revision="snapshot",
        license="MIT",
        license_url="https://user@example.test/LICENSE",
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status="license",
    )

    with pytest.raises(ValueError, match="credentials are not allowed"):
        validate_import_plan(source)


def test_registry_rejects_duplicate_canonical_source_urls() -> None:
    first = ExternalSourceRecord(
        name="first",
        url="https://example.test/catalog/~tool/",
        revision="one",
        license="MIT",
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status="license",
    )
    second = replace(
        first,
        name="second",
        url="HTTPS://EXAMPLE.TEST:443/catalog/%7etool",
        revision="two",
    )

    with pytest.raises(ValueError, match="duplicate canonical source URL"):
        validate_source_registry((first, second))


def test_full_body_requires_immutable_license_evidence() -> None:
    source = ExternalSourceRecord(
        name="mutable-evidence",
        url="https://example.test/source",
        revision=MATT_REVISION,
        license="MIT",
        license_url=f"https://example.test/{MATT_REVISION}/LICENSE",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="license",
        redistribution_obligations=RETAIN_MIT,
        notice_reference=f"https://example.test/{MATT_REVISION}/LICENSE",
    )

    with pytest.raises(LicenseGateError, match="checked-in license evidence"):
        validate_import_plan(source)

    permitted = replace(
        source,
        license_evidence_sha256=MATT_LICENSE_SHA256.upper(),
        license_evidence_path=MATT_LICENSE_PATH,
    )
    assert validate_import_plan(permitted).license_evidence_sha256 == MATT_LICENSE_SHA256


@pytest.mark.parametrize("license_name", ["CC-BY-4.0", "CC0-1.0", "MPL-2.0"])
def test_cc_and_mpl_are_not_unconditionally_allowed_for_full_body(
    license_name: str,
) -> None:
    source = ExternalSourceRecord(
        name="review-required",
        url="https://example.test/source",
        revision="a" * 40,
        license=license_name,
        license_url=f"https://example.test/{'a' * 40}/LICENSE",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="license",
        redistribution_obligations=("Review source-specific obligations.",),
        notice_reference="https://example.test/NOTICE",
    )

    with pytest.raises(LicenseGateError, match="unapproved license"):
        validate_import_plan(source)


def test_manifest_binding_rejects_license_drift() -> None:
    source = get_external_source("designdotmd")
    manifest: dict[str, Any] = {
        "upstream": source.url,
        "fetched_on": source.revision,
        "license": "MIT",
        "license_url": "https://example.test/LICENSE",
    }

    with pytest.raises(ValueError, match="manifest license does not match"):
        validate_ingestion_manifest(source, manifest, import_mode="full-body")


def test_manifest_binding_requires_registered_license_evidence_digest() -> None:
    revision = "b" * 40
    manifest = _ingestion_manifest(revision=revision)
    source = ExternalSourceRecord(
        name="future-source",
        url="https://example.test/source",
        revision=revision,
        license="MIT",
        license_url=f"https://example.test/{revision}/LICENSE",
        license_evidence_sha256=MATT_LICENSE_SHA256,
        license_evidence_path=MATT_LICENSE_PATH,
        manifest_sha256=canonical_ingestion_manifest_sha256(manifest),
        redistribution_obligations=RETAIN_MIT,
        notice_reference=f"https://example.test/{revision}/LICENSE",
        source_kind="skill-suite",
        import_mode="metadata-only",
        permission_status="license",
    )

    validated = validate_ingestion_manifest(source, manifest, import_mode=" FULL-BODY ")

    assert validated.import_mode == "full-body"
    assert validated.license_evidence_sha256 == MATT_LICENSE_SHA256

    manifest["license_evidence_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="license evidence digest does not match"):
        validate_ingestion_manifest(source, manifest, import_mode="full-body")


def test_full_manifest_binding_rejects_body_and_entry_digest_rewrite() -> None:
    revision = "b" * 40
    manifest = _ingestion_manifest(
        revision=revision,
        entries=[_manifest_entry(source_path="skill.md", sha256="1" * 64)],
    )
    source = ExternalSourceRecord(
        name="bound-source",
        url=manifest["upstream"],
        revision=revision,
        license="MIT",
        license_url=manifest["license_url"],
        license_evidence_sha256=MATT_LICENSE_SHA256,
        license_evidence_path=MATT_LICENSE_PATH,
        manifest_sha256=canonical_ingestion_manifest_sha256(manifest),
        redistribution_obligations=RETAIN_MIT,
        notice_reference=manifest["license_url"],
        source_kind="skill-suite",
        import_mode="metadata-only",
        permission_status="license",
    )

    manifest["entries"][0]["sha256"] = "2" * 64

    with pytest.raises(LicenseGateError, match="full-manifest binding mismatch"):
        validate_ingestion_manifest(source, manifest, import_mode="full-body")


@pytest.mark.parametrize(
    ("source_path", "message"),
    [
        (" skill.md", "canonical repository-relative POSIX path"),
        ("skill.md ", "canonical repository-relative POSIX path"),
        ("../skill.md", "canonical repository-relative POSIX path"),
        ("/skill.md", "canonical repository-relative POSIX path"),
        (r"skills\skill.md", "canonical repository-relative POSIX path"),
        ("skills//skill.md", "canonical repository-relative POSIX path"),
        (".", "canonical repository-relative POSIX path"),
        ("skills/e\u0301.md", "NFC-normalized"),
        ("skills/safe\u202eexe.md", "Unicode control, format"),
        ("skills/zero\u200dwidth.md", "Unicode control, format"),
        ("skills/\x00skill.md", "Unicode control, format"),
        ("skills/file.md:payload", "NTFS alternate data streams"),
        ("C:/skills/file.md", "NTFS alternate data streams"),
        ("skills/CON", "Windows-reserved"),
        ("skills/nul.txt", "Windows-reserved"),
        ("skills/COM1.md", "Windows-reserved"),
        ("skills/LPT\u00b2.log", "Windows-reserved"),
        ("skills/file./body.md", "trailing dots"),
        ("skills /body.md", "surrounding whitespace"),
        ("skills/file?.md", "Windows-unsafe"),
    ],
)
def test_full_manifest_binding_rejects_nonportable_source_paths(
    source_path: str,
    message: str,
) -> None:
    manifest = _ingestion_manifest(
        entries=[_manifest_entry(source_path=source_path)],
    )

    with pytest.raises(ValueError, match=message):
        canonical_ingestion_manifest_sha256(manifest)


@pytest.mark.parametrize(
    "source_path",
    [
        "skills/design.md",
        "skills/\u00e9clair.md",
        "skills/\u8bbe\u8ba1.md",
        ".well-known/design.md",
        "skills/COM10.md",
        "skills/auxiliary.md",
        "skills/file name.md",
    ],
)
def test_full_manifest_binding_accepts_safe_portable_source_paths(
    source_path: str,
) -> None:
    manifest = _ingestion_manifest(
        entries=[_manifest_entry(source_path=source_path)],
    )

    assert len(canonical_ingestion_manifest_sha256(manifest)) == 64


def test_full_manifest_binding_rejects_casefold_duplicate_source_paths() -> None:
    manifest = _ingestion_manifest(
        entries=[
            _manifest_entry(slug="first", source_path="skill.md", sha256="1" * 64),
            _manifest_entry(
                slug="second",
                source_path="SKILL.md",
                sha256="2" * 64,
            ),
        ],
    )

    with pytest.raises(ValueError, match="duplicate source path"):
        canonical_ingestion_manifest_sha256(manifest)


def test_full_manifest_binding_is_deterministic_across_object_key_order() -> None:
    manifest = _ingestion_manifest()
    reordered = dict(reversed(list(manifest.items())))
    reordered["entries"] = [dict(reversed(list(entry.items()))) for entry in manifest["entries"]]

    assert canonical_ingestion_manifest_sha256(reordered) == (
        canonical_ingestion_manifest_sha256(manifest)
    )


@pytest.mark.parametrize(
    ("permission_reference", "permission_digest", "message"),
    [
        ("trust me", "1" * 64, "canonical checked-in path"),
        (f"https://example.test/{'a' * 40}/permission.txt", "0" * 64, "all-zero"),
        (
            f"https://example.test/{'a' * 40}/{'1' * 64}/permission.txt",
            "1" * 64,
            "checked-in",
        ),
    ],
)
def test_explicit_permission_rejects_unverifiable_evidence(
    permission_reference: str,
    permission_digest: str,
    message: str,
) -> None:
    source = ExternalSourceRecord(
        name="unverified-permission",
        url="https://example.test/source",
        revision="a" * 40,
        license="unknown",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="explicit-permission",
        permission_reference=permission_reference,
        permission_evidence_sha256=permission_digest,
        redistribution_obligations=("preserve-attribution",),
        notice_reference=(
            permission_reference if permission_reference.startswith("https://") else None
        ),
    )

    with pytest.raises((ValueError, LicenseGateError), match=message):
        validate_import_plan(source)


def test_checked_in_license_evidence_must_match_digest_and_declared_license() -> None:
    base = ExternalSourceRecord(
        name="evidence-mismatch",
        url="https://example.test/source",
        revision=MATT_REVISION,
        license="MIT",
        license_url=f"https://example.test/{MATT_REVISION}/LICENSE",
        license_evidence_sha256="f" * 64,
        license_evidence_path=MATT_LICENSE_PATH,
        redistribution_obligations=RETAIN_MIT,
        notice_reference=f"https://example.test/{MATT_REVISION}/LICENSE",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="license",
    )
    with pytest.raises(LicenseGateError, match="evidence sha256 mismatch"):
        validate_import_plan(base)

    wrong_identity = replace(
        base,
        license="Apache-2.0",
        license_evidence_sha256=MATT_LICENSE_SHA256,
        redistribution_obligations=("retain-license-notice",),
    )
    with pytest.raises(LicenseGateError, match="does not identify declared license"):
        validate_import_plan(wrong_identity)


def test_full_body_rejects_free_form_redistribution_obligations() -> None:
    source = ExternalSourceRecord(
        name="free-form-obligation",
        url="https://example.test/source",
        revision=MATT_REVISION,
        license="MIT",
        license_url=f"https://example.test/{MATT_REVISION}/LICENSE",
        license_evidence_sha256=MATT_LICENSE_SHA256,
        license_evidence_path=MATT_LICENSE_PATH,
        redistribution_obligations=("anything",),
        notice_reference=f"https://example.test/{MATT_REVISION}/LICENSE",
        source_kind="skill-suite",
        import_mode="full-body",
        permission_status="license",
    )

    with pytest.raises(LicenseGateError, match="registered control identifiers"):
        validate_import_plan(source)


@pytest.mark.parametrize(
    "license_name",
    ["Unlicense", "The Unlicense", "no-explicit-license"],
)
def test_license_normalization_preserves_unlicense_identities(license_name: str) -> None:
    source = ExternalSourceRecord(
        name="license-identity",
        url="https://example.test/license-identity",
        revision="snapshot",
        license=license_name,
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status=("unknown" if license_name == "no-explicit-license" else "license"),
    )

    assert validate_import_plan(source).license == license_name


@pytest.mark.parametrize(
    ("registered_license", "manifest_license", "permission_status"),
    [
        ("Apache-2.0", "Apache License 2.0", "license"),
        ("Unlicense", "The Unlicense", "license"),
        ("no-explicit-license", "No Explicit License", "unknown"),
    ],
)
def test_manifest_license_aliases_share_a_canonical_identity(
    registered_license: str,
    manifest_license: str,
    permission_status: str,
) -> None:
    source = ExternalSourceRecord(
        name="license-alias",
        url="https://example.test/license-alias",
        revision="snapshot",
        license=registered_license,
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status=permission_status,
    )
    manifest = {
        "upstream": source.url,
        "upstream_revision": source.revision,
        "license": manifest_license,
    }

    assert (
        validate_ingestion_manifest(source, manifest, import_mode="metadata-only").license
        == registered_license
    )


@pytest.mark.parametrize("name", ["Tool", "tool_name", "tool--name", "tool]"])
def test_source_names_must_be_canonical_lowercase_slugs(name: str) -> None:
    source = ExternalSourceRecord(
        name=name,
        url="https://example.test/source",
        revision="snapshot",
        license="MIT",
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status="license",
    )

    with pytest.raises(ValueError, match="canonical lowercase slug"):
        validate_import_plan(source)


def test_notice_escapes_markdown_labels_and_link_destinations() -> None:
    source = ExternalSourceRecord(
        name="safe-source",
        url="https://example.test/catalog/(stable)",
        revision="<snapshot>",
        license="<script>alert(1)</script> MIT](https://attacker.invalid",
        source_kind="catalog",
        import_mode="metadata-only",
        permission_status="license",
        notes="<img src=x onerror=alert(1)> safe ](https://attacker.invalid) text & more",
    )

    notice = render_third_party_notice((source,))

    assert "&lt;script&gt;alert(1)&lt;/script&gt; MIT\\](https://attacker.invalid" in notice
    assert (
        "&lt;img src=x onerror=alert(1)&gt; safe \\](https://attacker.invalid) text &amp; more"
    ) in notice
    assert "&lt;snapshot&gt;" in notice
    assert "catalog/%28stable%29" in notice
    assert "<script>" not in notice
    assert "<img " not in notice
    assert "| MIT](https://attacker.invalid" not in notice
    assert "| safe ](https://attacker.invalid)" not in notice


def test_entity_provenance_is_complete_and_explicit() -> None:
    source = get_external_source("mattpocock-skills")

    assert entity_provenance(source) == {
        "source": source.name,
        "source_url": source.url,
        "source_revision": source.revision,
        "license": source.license,
        "license_url": source.license_url,
        "import_mode": source.import_mode,
        "permission_status": source.permission_status,
        "permission_reference": source.permission_reference,
        "permission_evidence_sha256": source.permission_evidence_sha256,
        "license_evidence_sha256": source.license_evidence_sha256,
        "license_evidence_path": source.license_evidence_path,
        "manifest_sha256": source.manifest_sha256,
        "redistribution_obligations": list(source.redistribution_obligations),
        "notice_reference": source.notice_reference,
    }


def test_checked_in_notice_is_generated_from_registry() -> None:
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")

    assert notice == render_third_party_notice(BUILTIN_EXTERNAL_SOURCES)
    assert "fail-closed engineering provenance inventory, not legal advice" in notice
    assert "does not prove" in notice
    assert "minimum engineering safeguards, not exhaustive legal" in notice
    assert "Permission evidence" in notice
    assert "Manifest SHA-256" in notice
    assert "Redistribution controls" in notice
    assert "| [designdotmd]" in notice
    assert "Existing unresolved corpus remains blocked" in notice


def test_load_source_registry_validates_json_records(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.json"
    registry_path.write_text(
        json.dumps(
            [
                {
                    "name": "lat-md",
                    "url": "HTTPS://GITHUB.COM:443/1st1/lat.md/",
                    "revision": "bf8d95c",
                    "license": "MIT",
                    "license_url": "https://example.test/lat-md/LICENSE/",
                    "source_kind": "knowledge-protocol",
                    "import_mode": " Metadata-Only ",
                    "permission_status": " License ",
                    "permission_reference": " ",
                }
            ],
        ),
        encoding="utf-8",
    )

    records = load_source_registry(registry_path)

    assert [record.name for record in records] == ["lat-md"]
    assert records[0].url == "https://github.com/1st1/lat.md"
    assert records[0].license_url == "https://example.test/lat-md/LICENSE"
    assert records[0].import_mode == "metadata-only"
    assert records[0].permission_status == "license"
    assert records[0].permission_reference is None
