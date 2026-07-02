"""External source registry and license gate for graph/wiki ingestion."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

PERMISSIVE_LICENSES = {
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "cc-by-4.0",
    "cc0-1.0",
    "isc",
    "mit",
    "mpl-2.0",
    "unlicense",
}
FULL_IMPORT_MODES = {"full", "full-body"}
SAFE_IMPORT_MODES = {"external-link", "metadata-only"}
ALLOWED_IMPORT_MODES = FULL_IMPORT_MODES | SAFE_IMPORT_MODES
EXPLICIT_PERMISSION_STATUSES = {"explicit-permission", "owner-permission"}


class LicenseGateError(ValueError):
    """Raised when a source cannot be imported with the requested mode."""


@dataclass(frozen=True)
class ExternalSourceRecord:
    name: str
    url: str
    revision: str
    license: str
    source_kind: str
    import_mode: str
    permission_status: str
    permission_reference: str | None = None
    notes: str | None = None

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "ExternalSourceRecord":
        required = (
            "name",
            "url",
            "revision",
            "license",
            "source_kind",
            "import_mode",
            "permission_status",
        )
        missing = [field for field in required if not str(raw.get(field) or "").strip()]
        if missing:
            raise ValueError(f"source record missing required field(s): {', '.join(missing)}")
        return cls(
            name=_string(raw["name"], "name"),
            url=_string(raw["url"], "url"),
            revision=_string(raw["revision"], "revision"),
            license=_string(raw["license"], "license"),
            source_kind=_string(raw["source_kind"], "source_kind"),
            import_mode=_string(raw["import_mode"], "import_mode"),
            permission_status=_string(raw["permission_status"], "permission_status"),
            permission_reference=_optional_string(raw.get("permission_reference")),
            notes=_optional_string(raw.get("notes")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


BUILTIN_EXTERNAL_SOURCES: tuple[ExternalSourceRecord, ...] = (
    ExternalSourceRecord(
        name="mattpocock-skills",
        url="https://github.com/mattpocock/skills",
        revision="e74f0061bb67222181640effa98c675bdb2fdaa7",
        license="MIT",
        source_kind="skill-suite",
        import_mode="full",
        permission_status="license",
    ),
    ExternalSourceRecord(
        name="academic-research-skills",
        url="https://github.com/Imbad0202/academic-research-skills",
        revision="153203d129b1d0e83dd65ab96340048257cd45b2",
        license="CC BY-NC 4.0",
        source_kind="skill-suite",
        import_mode="metadata-only",
        permission_status="license",
        notes="Full-body import requires explicit noncommercial-license permission.",
    ),
    ExternalSourceRecord(
        name="agents-md",
        url="https://github.com/agentsmd/agents.md",
        revision="d1ac7f063d20e70015ed6732664049ae4ba9d74e",
        license="MIT",
        source_kind="knowledge-protocol",
        import_mode="metadata-only",
        permission_status="license",
    ),
    ExternalSourceRecord(
        name="lat-md",
        url="https://github.com/1st1/lat.md",
        revision="bf8d95ca7ece6e1a9e4a325eddb51ddd5db038b0",
        license="MIT",
        source_kind="knowledge-protocol",
        import_mode="metadata-only",
        permission_status="license",
    ),
    ExternalSourceRecord(
        name="optillm",
        url="https://github.com/algorithmicsuperintelligence/optillm",
        revision="df018d64db96d07fdd338d71a35fc567f9d50c7b",
        license="Apache-2.0",
        source_kind="harness",
        import_mode="metadata-only",
        permission_status="license",
    ),
    ExternalSourceRecord(
        name="julius-caveman",
        url="https://github.com/JuliusBrussee/caveman",
        revision="63a91ecadbf4c4719a4602a5abb00883f9966034",
        license="MIT",
        source_kind="skill-suite",
        import_mode="full",
        permission_status="license",
    ),
)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: expected non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string field must be a string when set")
    return value.strip() or None


def _normalize_license(value: str) -> str:
    return value.lower().replace("_", "-").replace(" ", "-").replace("license", "").strip("-")


def _license_block_reason(record: ExternalSourceRecord) -> str | None:
    license_norm = _normalize_license(record.license)
    if license_norm in PERMISSIVE_LICENSES:
        return None
    if "noncommercial" in license_norm or "-nc" in license_norm:
        return "non-commercial license"
    if "unknown" in license_norm or "no-explicit" in license_norm:
        return "unknown license"
    if "gpl" in license_norm or "agpl" in license_norm or "lgpl" in license_norm:
        return "copyleft license"
    return f"unapproved license {record.license!r}"


def validate_import_plan(record: ExternalSourceRecord) -> ExternalSourceRecord:
    """Validate whether a source may be imported into shipped graph/wiki artifacts."""

    import_mode = record.import_mode.strip().lower()
    if import_mode not in ALLOWED_IMPORT_MODES:
        raise ValueError(
            f"{record.name}: import_mode must be one of {sorted(ALLOWED_IMPORT_MODES)}",
        )
    if import_mode in SAFE_IMPORT_MODES:
        return record

    reason = _license_block_reason(record)
    if reason is None:
        return record

    has_permission = record.permission_status in EXPLICIT_PERMISSION_STATUSES
    has_reference = bool(record.permission_reference)
    if has_permission and has_reference:
        return record

    raise LicenseGateError(
        f"{record.name}: full-body import blocked by {reason}; use metadata-only "
        "or record explicit permission with permission_reference.",
    )


def load_source_registry(path: Path) -> list[ExternalSourceRecord]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records_raw: Iterable[Any]
    if isinstance(raw, dict):
        records_raw = raw.get("sources", [])
    else:
        records_raw = raw
    if not isinstance(records_raw, list):
        raise ValueError("source registry must be a list or an object with a sources list")
    records = [ExternalSourceRecord.from_mapping(item) for item in records_raw]
    for record in records:
        validate_import_plan(record)
    return records


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate ctx external source import policy.")
    parser.add_argument("--registry", type=Path, help="Optional JSON registry path.")
    parser.add_argument("--json", action="store_true", help="Emit records as JSON.")
    args = parser.parse_args(argv)

    records = (
        load_source_registry(args.registry) if args.registry else list(BUILTIN_EXTERNAL_SOURCES)
    )
    for record in records:
        validate_import_plan(record)

    if args.json:
        print(json.dumps({"sources": [record.to_dict() for record in records]}, indent=2))
    else:
        print(f"Validated {len(records)} external source record(s).")


if __name__ == "__main__":
    main()
