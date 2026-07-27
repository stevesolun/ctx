"""External source registry and license gate for graph/wiki ingestion."""

from __future__ import annotations

import argparse
import base64
import binascii
import functools
import gzip
import hashlib
from importlib import resources
from ipaddress import IPv6Address
import json
import re
import secrets
import unicodedata
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit, urlunsplit

FULL_BODY_LICENSE_ALLOWLIST = {
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "isc",
    "mit",
    "unlicense",
}
FULL_IMPORT_MODES = {"full", "full-body"}
SAFE_IMPORT_MODES = {"external-link", "metadata-only"}
ALLOWED_IMPORT_MODES = FULL_IMPORT_MODES | SAFE_IMPORT_MODES
EXPLICIT_PERMISSION_STATUSES = {"explicit-permission", "owner-permission"}
KNOWN_LICENSE_PERMISSION_STATUS = "license"
UNKNOWN_LICENSE_PERMISSION_STATUS = "unknown"
ALLOWED_PERMISSION_STATUSES = {
    KNOWN_LICENSE_PERMISSION_STATUS,
    UNKNOWN_LICENSE_PERMISSION_STATUS,
    *EXPLICIT_PERMISSION_STATUSES,
}
UNKNOWN_LICENSES = {"unknown", "no-explicit-license"}
FULL_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
INGESTION_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
INGESTION_MANIFEST_SCHEMA = "ctx.ingestion-manifest.v1"
JSON_SAFE_INTEGER_MAX = (1 << 53) - 1
UNRESERVED_URL_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~",
)
WINDOWS_UNSAFE_PATH_CHARACTERS = frozenset('<>:"|?*')
WINDOWS_RESERVED_PATH_STEMS = frozenset(
    {
        "aux",
        "clock$",
        "con",
        "conin$",
        "conout$",
        "nul",
        "prn",
    }
    | {f"com{suffix}" for suffix in (*map(str, range(1, 10)), "\u00b9", "\u00b2", "\u00b3")}
    | {f"lpt{suffix}" for suffix in (*map(str, range(1, 10)), "\u00b9", "\u00b2", "\u00b3")}
)
REPO_ROOT = Path(__file__).resolve().parents[3]
SPDX_LICENSE_ALIASES = {
    "apache-2": "apache-2.0",
    "apache-license-2.0": "apache-2.0",
    "apache-license-version-2.0": "apache-2.0",
    "apache-2.0-license": "apache-2.0",
    "bsd-2-clause-license": "bsd-2-clause",
    "bsd-3-clause-license": "bsd-3-clause",
    "cc-by-nc-4": "cc-by-nc-4.0",
    "mit-license": "mit",
    "the-unlicense": "unlicense",
}
REDISTRIBUTION_OBLIGATION_LABELS = {
    "include-upstream-notice": "Include applicable upstream NOTICE material.",
    "mark-modifications": "Mark material modifications.",
    "preserve-attribution": "Preserve required upstream attribution.",
    "retain-copyright-notice": "Retain the upstream copyright notice.",
    "retain-license-notice": "Retain the upstream license notice.",
}
MARKDOWN_UNSAFE_URL_CHARACTERS = {
    "(": "%28",
    ")": "%29",
    "[": "%5B",
    "]": "%5D",
    "<": "%3C",
    ">": "%3E",
    '"': "%22",
}


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
    license_url: str | None = None
    permission_reference: str | None = None
    permission_evidence_sha256: str | None = None
    license_evidence_sha256: str | None = None
    license_evidence_path: str | None = None
    manifest_sha256: str | None = None
    redistribution_obligations: tuple[str, ...] = ()
    notice_reference: str | None = None
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
            license_url=_optional_string(raw.get("license_url")),
            permission_reference=_optional_string(raw.get("permission_reference")),
            permission_evidence_sha256=_optional_string(
                raw.get("permission_evidence_sha256"),
            ),
            license_evidence_sha256=_optional_string(raw.get("license_evidence_sha256")),
            license_evidence_path=_optional_string(raw.get("license_evidence_path")),
            manifest_sha256=_optional_string(raw.get("manifest_sha256")),
            redistribution_obligations=_string_tuple(
                raw.get("redistribution_obligations"),
                "redistribution_obligations",
            ),
            notice_reference=_optional_string(raw.get("notice_reference")),
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
        license_url=(
            "https://github.com/mattpocock/skills/blob/"
            "e74f0061bb67222181640effa98c675bdb2fdaa7/LICENSE"
        ),
        license_evidence_sha256=(
            "0e7ac423bf2c6e223b7c5b156f8cf72da49d748e56a1641402c31f22ad07dbb5"
        ),
        license_evidence_path="imported-skills/mattpocock/LICENSE",
        redistribution_obligations=(
            "retain-copyright-notice",
            "retain-license-notice",
        ),
        notice_reference=(
            "https://github.com/mattpocock/skills/blob/"
            "e74f0061bb67222181640effa98c675bdb2fdaa7/LICENSE"
        ),
    ),
    ExternalSourceRecord(
        name="academic-research-skills",
        url="https://github.com/Imbad0202/academic-research-skills",
        revision="153203d129b1d0e83dd65ab96340048257cd45b2",
        license="CC-BY-NC-4.0",
        source_kind="skill-suite",
        import_mode="metadata-only",
        permission_status="license",
        license_url=(
            "https://github.com/Imbad0202/academic-research-skills/blob/"
            "153203d129b1d0e83dd65ab96340048257cd45b2/LICENSE"
        ),
        redistribution_obligations=(
            "Do not redistribute full bodies under this registry record.",
            "Preserve source and declared noncommercial-license links in metadata.",
        ),
        notice_reference=(
            "https://github.com/Imbad0202/academic-research-skills/blob/"
            "153203d129b1d0e83dd65ab96340048257cd45b2/LICENSE"
        ),
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
        license_url=(
            "https://github.com/agentsmd/agents.md/blob/"
            "d1ac7f063d20e70015ed6732664049ae4ba9d74e/LICENSE"
        ),
        redistribution_obligations=(
            "Limit this registry record to metadata and external links.",
            "Retain source and declared-license links in derived metadata.",
        ),
        notice_reference=(
            "https://github.com/agentsmd/agents.md/blob/"
            "d1ac7f063d20e70015ed6732664049ae4ba9d74e/LICENSE"
        ),
    ),
    ExternalSourceRecord(
        name="lat-md",
        url="https://github.com/1st1/lat.md",
        revision="bf8d95ca7ece6e1a9e4a325eddb51ddd5db038b0",
        license="MIT",
        source_kind="knowledge-protocol",
        import_mode="metadata-only",
        permission_status="license",
        license_url=(
            "https://github.com/1st1/lat.md/blob/bf8d95ca7ece6e1a9e4a325eddb51ddd5db038b0/LICENSE"
        ),
        redistribution_obligations=(
            "Limit this registry record to metadata and external links.",
            "Retain source and declared-license links in derived metadata.",
        ),
        notice_reference=(
            "https://github.com/1st1/lat.md/blob/bf8d95ca7ece6e1a9e4a325eddb51ddd5db038b0/LICENSE"
        ),
    ),
    ExternalSourceRecord(
        name="optillm",
        url="https://github.com/algorithmicsuperintelligence/optillm",
        revision="df018d64db96d07fdd338d71a35fc567f9d50c7b",
        license="Apache-2.0",
        source_kind="harness",
        import_mode="metadata-only",
        permission_status="license",
        license_url=(
            "https://github.com/algorithmicsuperintelligence/optillm/blob/"
            "df018d64db96d07fdd338d71a35fc567f9d50c7b/LICENSE"
        ),
        redistribution_obligations=(
            "Limit this registry record to metadata and external links.",
            "Retain source and declared-license links in derived metadata.",
        ),
        notice_reference=(
            "https://github.com/algorithmicsuperintelligence/optillm/blob/"
            "df018d64db96d07fdd338d71a35fc567f9d50c7b/LICENSE"
        ),
    ),
    ExternalSourceRecord(
        name="julius-caveman",
        url="https://github.com/JuliusBrussee/caveman",
        revision="63a91ecadbf4c4719a4602a5abb00883f9966034",
        license="MIT",
        source_kind="skill-suite",
        import_mode="full",
        permission_status="license",
        license_url=(
            "https://github.com/JuliusBrussee/caveman/blob/"
            "63a91ecadbf4c4719a4602a5abb00883f9966034/LICENSE"
        ),
        license_evidence_sha256=(
            "5eb826cd03151bcc7cce3f80d40e87733237fedfc6c36d6908aca5fd650a0bdb"
        ),
        license_evidence_path="imported-skills/julius-caveman/LICENSE",
        redistribution_obligations=(
            "retain-copyright-notice",
            "retain-license-notice",
        ),
        notice_reference=(
            "https://github.com/JuliusBrussee/caveman/blob/"
            "63a91ecadbf4c4719a4602a5abb00883f9966034/LICENSE"
        ),
    ),
    ExternalSourceRecord(
        name="strix",
        url="https://github.com/usestrix/strix",
        revision="15c95718e600897a2a532a613a1c8fa6b712b144",
        license="Apache-2.0",
        source_kind="skill-suite",
        import_mode="full",
        permission_status="license",
        license_url=(
            "https://github.com/usestrix/strix/blob/"
            "15c95718e600897a2a532a613a1c8fa6b712b144/LICENSE"
        ),
        license_evidence_sha256=(
            "7db9697134251e314bd8d39257fe170c98b3e2ad9b2ed67e97acaf9613e7b9e3"
        ),
        license_evidence_path="imported-skills/strix/LICENSE",
        redistribution_obligations=(
            "include-upstream-notice",
            "mark-modifications",
            "preserve-attribution",
            "retain-license-notice",
        ),
        notice_reference=(
            "https://github.com/usestrix/strix/blob/"
            "15c95718e600897a2a532a613a1c8fa6b712b144/LICENSE"
        ),
    ),
    ExternalSourceRecord(
        name="no-mistakes",
        url="https://github.com/kunchenguid/no-mistakes",
        revision="unverified-snapshot",
        license="MIT",
        source_kind="workflow-reference",
        import_mode="metadata-only",
        permission_status="license",
        license_url="https://github.com/kunchenguid/no-mistakes/blob/main/LICENSE",
        redistribution_obligations=(
            "Limit this registry record to metadata and external links.",
            "Do not infer permission to redistribute upstream bodies from this record.",
        ),
        notice_reference="https://github.com/kunchenguid/no-mistakes/blob/main/LICENSE",
        notes="The checked-in ctx integration guide is not recorded as a verbatim upstream body.",
    ),
    ExternalSourceRecord(
        name="designdotmd",
        url="https://designdotmd.directory",
        revision="2026-04-27",
        license="unknown",
        source_kind="design-catalog",
        import_mode="metadata-only",
        permission_status="unknown",
        redistribution_obligations=(
            "Do not redistribute source bodies until permission and legal review are recorded.",
            "Limit use to metadata and external links while license status is unresolved.",
        ),
        notice_reference="https://designdotmd.directory",
        notes=(
            "No explicit license found; full-body ingestion is blocked. Existing mirrored "
            "bodies require quarantine or permission before redistribution."
        ),
    ),
    ExternalSourceRecord(
        name="skills-sh",
        url="https://skills.sh",
        revision="2026-05-18",
        license="unknown",
        source_kind="external-catalog",
        import_mode="metadata-only",
        permission_status="unknown",
        redistribution_obligations=(
            "Do not redistribute source bodies without per-entity license provenance.",
            "Limit this catalog record to metadata and external links.",
        ),
        notice_reference="https://skills.sh",
        notes=(
            "Catalog metadata only; this record does not authorize redistribution "
            "of per-repository skill bodies. Existing body artifacts require per-entity "
            "provenance before redistribution."
        ),
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


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    values: tuple[object, ...]
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(value)
    else:
        raise ValueError(f"{field}: expected a string or list of strings")
    return tuple(_string(item, field) for item in values)


def _normalize_license(value: str) -> str:
    normalized = re.sub(r"[-\s_]+", "-", value.strip().lower()).strip("-")
    if normalized.endswith("-license") and normalized not in {
        "no-explicit-license",
    }:
        normalized = normalized.removesuffix("-license")
    return SPDX_LICENSE_ALIASES.get(normalized, normalized)


def _canonical_source_name(value: object) -> str:
    name = _string(value, "name")
    if not SOURCE_NAME_RE.fullmatch(name):
        raise ValueError(
            "name: expected a canonical lowercase slug containing only letters, "
            "digits, and single hyphens",
        )
    return name


def _canonical_percent_escape(match: re.Match[str]) -> str:
    character = chr(int(match.group(0)[1:], 16))
    return character if character in UNRESERVED_URL_CHARACTERS else match.group(0).upper()


def _canonical_https_url(value: object, field: str) -> str:
    raw = _string(value, field)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in raw
    ):
        raise ValueError(f"{field}: URL must not contain whitespace or control characters")
    if "\\" in raw or "?" in raw or "#" in raw:
        raise ValueError(f"{field}: URL must not contain backslashes, query, or fragment")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field}: invalid URL: {exc}") from None
    if parsed.scheme.lower() != "https" or not parsed.netloc or not hostname:
        raise ValueError(f"{field}: URL must use https and include a host")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError(f"{field}: URL credentials are not allowed")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{field}: URL query and fragment are not allowed")

    hostname = hostname.rstrip(".").lower()
    if not hostname:
        raise ValueError(f"{field}: URL must include a host")
    if ":" in hostname:
        try:
            canonical_host = f"[{IPv6Address(hostname).compressed}]"
        except ValueError:
            raise ValueError(f"{field}: invalid IPv6 host") from None
    else:
        try:
            canonical_host = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise ValueError(f"{field}: invalid host") from None

    path = parsed.path
    if INVALID_PERCENT_ESCAPE_RE.search(path):
        raise ValueError(f"{field}: URL path contains an invalid percent escape")
    decoded_path = unquote(path)
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in decoded_path
    ):
        raise ValueError(f"{field}: URL path must not encode whitespace or control characters")
    if "\\" in decoded_path or any(segment in {".", ".."} for segment in decoded_path.split("/")):
        raise ValueError(f"{field}: URL path contains an unsafe segment")
    canonical_path = PERCENT_ESCAPE_RE.sub(_canonical_percent_escape, path)
    canonical_path = "" if canonical_path == "/" else canonical_path.rstrip("/")
    netloc = canonical_host if port in (None, 443) else f"{canonical_host}:{port}"
    return urlunsplit(("https", netloc, canonical_path, "", ""))


def _optional_https_url(value: object, field: str) -> str | None:
    normalized = _optional_string(value)
    return None if normalized is None else _canonical_https_url(normalized, field)


def _optional_sha256(value: object, field: str) -> str | None:
    normalized = _optional_string(value)
    if normalized is None:
        return None
    normalized = normalized.lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{field}: expected a 64-character sha256 digest")
    if normalized == "0" * 64:
        raise ValueError(f"{field}: all-zero sha256 placeholders are not evidence")
    return normalized


def _canonical_evidence_path(value: object, field: str) -> str:
    raw = _string(value, field)
    candidate = Path(raw)
    if candidate.is_absolute() or not candidate.parts:
        raise ValueError(f"{field}: expected a repository-relative evidence path")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError(f"{field}: path traversal is not allowed")
    normalized = candidate.as_posix()
    if normalized != raw or not normalized.startswith("imported-skills/"):
        raise ValueError(
            f"{field}: expected a canonical checked-in path under imported-skills/",
        )
    return normalized


@functools.cache
def _packaged_evidence() -> dict[str, bytes]:
    try:
        raw = resources.files("ctx.assets").joinpath("license-evidence.json").read_text(
            encoding="utf-8",
        )
        packaged = json.loads(raw)
        if not isinstance(packaged, dict) or not isinstance(packaged.get("gzip_base64"), str):
            raise ValueError("expected gzip_base64")
        compressed = base64.b64decode(packaged["gzip_base64"], validate=True)
        encoded = json.loads(gzip.decompress(compressed))
        if not isinstance(encoded, dict):
            raise ValueError("expected an object")
        if not all(
            isinstance(path, str) and isinstance(value, str) for path, value in encoded.items()
        ):
            raise ValueError("expected string evidence entries")
        return {path: base64.b64decode(value, validate=True) for path, value in encoded.items()}
    except (EOFError, OSError, UnicodeError, ValueError, binascii.Error) as exc:
        raise LicenseGateError(f"packaged evidence is unavailable: {exc}") from None


def _verify_checked_in_evidence(path_value: str, digest: str, field: str) -> bytes:
    evidence_path = REPO_ROOT / path_value
    try:
        resolved = evidence_path.resolve(strict=True)
        repository = REPO_ROOT.resolve(strict=True)
    except FileNotFoundError:
        try:
            payload = _packaged_evidence()[path_value]
        except KeyError:
            raise LicenseGateError(f"{field}: evidence file is unavailable") from None
        actual = hashlib.sha256(payload).hexdigest()
        if not secrets.compare_digest(actual, digest):
            raise LicenseGateError(
                f"{field}: evidence sha256 mismatch; expected={digest}, actual={actual}",
            )
        return payload
    except OSError as exc:
        raise LicenseGateError(f"{field}: evidence file is unavailable: {exc}") from None
    try:
        resolved.relative_to(repository)
    except ValueError:
        raise LicenseGateError(f"{field}: evidence path escapes the repository") from None
    if evidence_path.is_symlink() or not resolved.is_file():
        raise LicenseGateError(f"{field}: evidence must be a regular checked-in file")
    payload = resolved.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if not secrets.compare_digest(actual, digest):
        raise LicenseGateError(
            f"{field}: evidence sha256 mismatch; expected={digest}, actual={actual}",
        )
    return payload


def _license_identity_from_evidence(payload: bytes) -> str | None:
    text = payload.decode("utf-8", errors="replace").lower()
    if "apache license" in text and "version 2.0" in text:
        return "apache-2.0"
    if "mit license" in text and "permission is hereby granted" in text:
        return "mit"
    if "redistribution and use in source and binary forms" in text and "neither the name" in text:
        return "bsd-3-clause"
    if "redistribution and use in source and binary forms" in text:
        return "bsd-2-clause"
    if "permission to use, copy, modify, and/or distribute this software" in text:
        return "isc"
    if "free and unencumbered software released into the public domain" in text:
        return "unlicense"
    return None


def _canonical_manifest_source_path(value: object, field: str) -> str:
    raw = _string(value, field)
    if not isinstance(value, str) or value != raw:
        raise ValueError(f"{field}: expected a canonical repository-relative POSIX path")
    if unicodedata.normalize("NFC", raw) != raw:
        raise ValueError(f"{field}: source path must be NFC-normalized")
    for character in raw:
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise ValueError(
                f"{field}: Unicode control, format, and surrogate characters are not allowed",
            )
    if ":" in raw:
        raise ValueError(f"{field}: colons and NTFS alternate data streams are not allowed")
    invalid_characters = sorted(set(raw) & WINDOWS_UNSAFE_PATH_CHARACTERS)
    if invalid_characters:
        raise ValueError(
            f"{field}: Windows-unsafe path character(s) are not allowed: {invalid_characters}",
        )
    candidate = PurePosixPath(raw)
    if (
        "\\" in raw
        or not candidate.parts
        or candidate.is_absolute()
        or candidate.as_posix() != raw
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"{field}: expected a canonical repository-relative POSIX path")
    for part in candidate.parts:
        if part[0].isspace() or part[-1].isspace() or part.endswith("."):
            raise ValueError(
                f"{field}: path components must not have ambiguous surrounding "
                "whitespace or trailing dots",
            )
        windows_stem = part.split(".", 1)[0].rstrip(" .").casefold()
        if windows_stem in WINDOWS_RESERVED_PATH_STEMS:
            raise ValueError(
                f"{field}: Windows-reserved path component {part!r} is not allowed",
            )
    return raw


def _canonical_manifest_text(value: object, field: str) -> str:
    text = _string(value, field)
    if not isinstance(value, str) or value != text:
        raise ValueError(f"{field}: expected a canonical non-empty string")
    return text


def _canonical_json_value(value: object, field: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        if abs(value) > JSON_SAFE_INTEGER_MAX:
            raise ValueError(f"{field}: integer exceeds the portable JSON safe range")
        return value
    if isinstance(value, list):
        return [
            _canonical_json_value(item, f"{field}[{index}]") for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field}: object keys must be strings")
            canonical[key] = _canonical_json_value(item, f"{field}.{key}")
        return canonical
    raise ValueError(
        f"{field}: expected canonical JSON data, got {type(value).__name__}",
    )


def canonical_ingestion_manifest_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash a validated full ingestion manifest under a versioned schema."""

    upstream = _canonical_manifest_text(manifest.get("upstream"), "manifest.upstream")
    if _canonical_https_url(upstream, "manifest.upstream") != upstream:
        raise ValueError("manifest.upstream: expected a canonical HTTPS URL")
    _canonical_manifest_text(manifest.get("fetched_on"), "manifest.fetched_on")
    revision = manifest.get("upstream_revision")
    if revision is not None:
        _canonical_manifest_text(revision, "manifest.upstream_revision")
    _canonical_manifest_text(manifest.get("license"), "manifest.license")
    license_url = manifest.get("license_url")
    if license_url is not None:
        canonical_license_url = _canonical_manifest_text(
            license_url,
            "manifest.license_url",
        )
        if (
            _canonical_https_url(canonical_license_url, "manifest.license_url")
            != canonical_license_url
        ):
            raise ValueError("manifest.license_url: expected a canonical HTTPS URL")
    license_digest = manifest.get("license_evidence_sha256")
    if license_digest is not None:
        canonical_license_digest = _optional_sha256(
            license_digest,
            "manifest.license_evidence_sha256",
        )
        if license_digest != canonical_license_digest:
            raise ValueError(
                "manifest.license_evidence_sha256: expected a canonical lowercase digest",
            )
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("manifest.entries: expected a list for full-manifest binding")
    source_paths: set[str] = set()
    slugs: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, Mapping):
            raise ValueError(f"manifest.entries[{index}]: expected an object")
        entry_field = f"manifest.entries[{index}]"
        _canonical_manifest_text(entry.get("name"), f"{entry_field}.name")
        _canonical_manifest_text(entry.get("author"), f"{entry_field}.author")
        slug = _canonical_manifest_text(entry.get("slug"), f"{entry_field}.slug")
        if not INGESTION_SLUG_RE.fullmatch(slug):
            raise ValueError(
                f"{entry_field}.slug: expected a canonical lowercase ingestion slug",
            )
        if slug in slugs:
            raise ValueError(f"{entry_field}.slug: duplicate ingestion slug {slug!r}")
        slugs.add(slug)
        tags = entry.get("tags")
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise ValueError(f"{entry_field}.tags: expected a list of strings")
        source_path = _canonical_manifest_source_path(
            entry.get("source_path"),
            f"{entry_field}.source_path",
        )
        source_path_key = source_path.casefold()
        if source_path_key in source_paths:
            raise ValueError(
                f"{entry_field}.source_path: duplicate source path {source_path!r}",
            )
        source_paths.add(source_path_key)
        digest = _optional_sha256(
            entry.get("sha256"),
            f"{entry_field}.sha256",
        )
        if digest is None:
            raise ValueError(f"{entry_field}.sha256: digest is required")
        if entry.get("sha256") != digest:
            raise ValueError(f"{entry_field}.sha256: expected a canonical lowercase digest")

    payload = {
        "schema": INGESTION_MANIFEST_SCHEMA,
        "manifest": _canonical_json_value(manifest, "manifest"),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _optional_evidence_reference(value: object, field: str) -> str | None:
    normalized = _optional_string(value)
    if normalized is None:
        return None
    if "://" in normalized:
        return _canonical_https_url(normalized, field)
    return _canonical_evidence_path(normalized, field)


def _normalize_record(record: ExternalSourceRecord) -> ExternalSourceRecord:
    return replace(
        record,
        name=_canonical_source_name(record.name),
        url=_canonical_https_url(record.url, f"{record.name}.url"),
        revision=_string(record.revision, f"{record.name}.revision"),
        license=_string(record.license, f"{record.name}.license"),
        source_kind=_string(record.source_kind, f"{record.name}.source_kind"),
        import_mode=_string(record.import_mode, f"{record.name}.import_mode").lower(),
        permission_status=_string(
            record.permission_status,
            f"{record.name}.permission_status",
        ).lower(),
        license_url=_optional_https_url(record.license_url, f"{record.name}.license_url"),
        permission_reference=_optional_evidence_reference(
            record.permission_reference,
            f"{record.name}.permission_reference",
        ),
        permission_evidence_sha256=_optional_sha256(
            record.permission_evidence_sha256,
            f"{record.name}.permission_evidence_sha256",
        ),
        license_evidence_sha256=_optional_sha256(
            record.license_evidence_sha256,
            f"{record.name}.license_evidence_sha256",
        ),
        license_evidence_path=(
            None
            if _optional_string(record.license_evidence_path) is None
            else _canonical_evidence_path(
                record.license_evidence_path,
                f"{record.name}.license_evidence_path",
            )
        ),
        manifest_sha256=_optional_sha256(
            record.manifest_sha256,
            f"{record.name}.manifest_sha256",
        ),
        redistribution_obligations=_string_tuple(
            record.redistribution_obligations,
            f"{record.name}.redistribution_obligations",
        ),
        notice_reference=_optional_evidence_reference(
            record.notice_reference,
            f"{record.name}.notice_reference",
        ),
        notes=_optional_string(record.notes),
    )


def _is_unknown_license(value: str) -> bool:
    normalized = _normalize_license(value)
    return normalized in UNKNOWN_LICENSES or normalized.startswith("unknown-")


def _license_identity(value: str) -> str:
    return "unknown" if _is_unknown_license(value) else _normalize_license(value)


def _license_block_reason(record: ExternalSourceRecord) -> str | None:
    license_norm = _normalize_license(record.license)
    if license_norm in FULL_BODY_LICENSE_ALLOWLIST:
        return None
    if "noncommercial" in license_norm or "-nc" in license_norm:
        return "non-commercial license"
    if _is_unknown_license(record.license):
        return "unknown license"
    if "gpl" in license_norm or "agpl" in license_norm or "lgpl" in license_norm:
        return "copyleft license"
    return f"unapproved license {record.license!r}"


def _license_url_is_pinned(record: ExternalSourceRecord) -> bool:
    if not record.license_url:
        return False
    return record.revision in urlsplit(record.license_url).path.split("/")


def validate_import_plan(record: ExternalSourceRecord) -> ExternalSourceRecord:
    """Apply the engineering provenance gate; this is not a legal conclusion."""

    normalized = _normalize_record(record)
    if normalized.import_mode not in ALLOWED_IMPORT_MODES:
        raise ValueError(
            f"{normalized.name}: import_mode must be one of {sorted(ALLOWED_IMPORT_MODES)}",
        )
    if normalized.permission_status not in ALLOWED_PERMISSION_STATUSES:
        raise ValueError(
            f"{normalized.name}: permission_status must be one of "
            f"{sorted(ALLOWED_PERMISSION_STATUSES)}",
        )
    unknown_license = _is_unknown_license(normalized.license)
    if unknown_license and normalized.permission_status == KNOWN_LICENSE_PERMISSION_STATUS:
        raise ValueError(
            f"{normalized.name}: unknown license cannot use permission_status='license'",
        )
    if not unknown_license and normalized.permission_status == UNKNOWN_LICENSE_PERMISSION_STATUS:
        raise ValueError(
            f"{normalized.name}: known license cannot use permission_status='unknown'",
        )
    if normalized.permission_status in EXPLICIT_PERMISSION_STATUSES:
        if normalized.permission_reference is None:
            raise ValueError(
                f"{normalized.name}: {normalized.permission_status} requires permission_reference",
            )
        if normalized.permission_evidence_sha256 is None:
            raise ValueError(
                f"{normalized.name}: {normalized.permission_status} requires "
                "permission_evidence_sha256",
            )
        if normalized.permission_reference.startswith("https://"):
            raise LicenseGateError(
                f"{normalized.name}: permission_reference must be a checked-in "
                "digest-verified evidence file; remote references are not verified",
            )
        _verify_checked_in_evidence(
            normalized.permission_reference,
            normalized.permission_evidence_sha256,
            f"{normalized.name}.permission_reference",
        )
    elif (
        normalized.permission_reference is not None
        or normalized.permission_evidence_sha256 is not None
    ):
        raise ValueError(
            f"{normalized.name}: permission evidence requires an explicit permission status",
        )

    if normalized.import_mode in SAFE_IMPORT_MODES:
        return normalized
    if not unknown_license and not normalized.license_url:
        raise ValueError(f"{normalized.name}: known license requires license_url")

    reason = _license_block_reason(normalized)
    if reason is not None:
        has_permission = normalized.permission_status in EXPLICIT_PERMISSION_STATUSES
        has_reference = normalized.permission_reference is not None
        if not (has_permission and has_reference):
            raise LicenseGateError(
                f"{normalized.name}: full-body import blocked by {reason}; use metadata-only "
                "or record explicit permission with permission_reference.",
            )
    if not FULL_GIT_REVISION_RE.fullmatch(normalized.revision):
        raise LicenseGateError(
            f"{normalized.name}: full-body import requires an immutable 40-character git revision",
        )
    if not unknown_license:
        if not _license_url_is_pinned(normalized):
            raise LicenseGateError(
                f"{normalized.name}: full-body import requires a license_url pinned "
                "to the registered revision",
            )
        if not normalized.license_evidence_path or not normalized.license_evidence_sha256:
            raise LicenseGateError(
                f"{normalized.name}: full-body import requires a checked-in license "
                "evidence file and verified sha256",
            )
        evidence_payload = _verify_checked_in_evidence(
            normalized.license_evidence_path,
            normalized.license_evidence_sha256,
            f"{normalized.name}.license_evidence_path",
        )
        evidence_license = _license_identity_from_evidence(evidence_payload)
        if evidence_license != _normalize_license(normalized.license):
            raise LicenseGateError(
                f"{normalized.name}: checked-in license evidence does not identify "
                f"declared license {normalized.license!r}",
            )
    if not normalized.redistribution_obligations:
        raise LicenseGateError(
            f"{normalized.name}: full-body import requires redistribution_obligations",
        )
    invalid_obligations = sorted(
        set(normalized.redistribution_obligations) - REDISTRIBUTION_OBLIGATION_LABELS.keys(),
    )
    if invalid_obligations:
        raise LicenseGateError(
            f"{normalized.name}: full-body redistribution_obligations must use registered "
            f"control identifiers; invalid={invalid_obligations}",
        )
    if normalized.notice_reference is None:
        raise LicenseGateError(
            f"{normalized.name}: full-body import requires notice_reference",
        )
    immutable_notice_references = {
        reference
        for reference in (
            normalized.license_url,
            normalized.license_evidence_path,
            normalized.permission_reference,
        )
        if reference is not None
    }
    if normalized.notice_reference not in immutable_notice_references:
        raise LicenseGateError(
            f"{normalized.name}: notice_reference must reuse verified immutable "
            "license or permission evidence",
        )
    return normalized


def validate_source_registry(
    records: Iterable[ExternalSourceRecord],
) -> tuple[ExternalSourceRecord, ...]:
    """Validate source records and reject ambiguous names or canonical URLs."""

    validated: list[ExternalSourceRecord] = []
    names: set[str] = set()
    urls: set[str] = set()
    for record in records:
        normalized = validate_import_plan(record)
        name_key = normalized.name.casefold()
        if name_key in names:
            raise ValueError(f"duplicate source record name: {normalized.name}")
        if normalized.url in urls:
            raise ValueError(f"duplicate canonical source URL: {normalized.url}")
        names.add(name_key)
        urls.add(normalized.url)
        validated.append(normalized)
    return tuple(validated)


def get_external_source(name: str) -> ExternalSourceRecord:
    """Return one built-in source record by its stable provenance name."""

    for record in BUILTIN_EXTERNAL_SOURCES:
        if record.name == name:
            return validate_import_plan(record)
    raise KeyError(f"unknown external source: {name}")


def validate_ingestion_manifest(
    record: ExternalSourceRecord,
    manifest: Mapping[str, Any],
    *,
    import_mode: str,
) -> ExternalSourceRecord:
    """Bind an importer manifest to its registered source before reading bodies."""

    normalized = validate_import_plan(record)
    requested_mode = _string(import_mode, "import_mode").lower()
    declared_url = _canonical_https_url(manifest.get("upstream"), "manifest.upstream")
    declared_revision = _string(
        manifest.get("upstream_revision") or manifest.get("fetched_on"),
        "manifest.upstream_revision or manifest.fetched_on",
    )
    declared_license = _string(manifest.get("license"), "manifest.license")
    declared_license_url = _optional_https_url(
        manifest.get("license_url"),
        "manifest.license_url",
    )
    declared_evidence_sha256 = _optional_sha256(
        manifest.get("license_evidence_sha256"),
        "manifest.license_evidence_sha256",
    )
    if declared_url != normalized.url:
        raise ValueError(f"{normalized.name}: manifest upstream does not match source registry")
    if declared_revision != normalized.revision:
        raise ValueError(f"{normalized.name}: manifest revision does not match source registry")
    if _license_identity(declared_license) != _license_identity(normalized.license):
        raise ValueError(f"{normalized.name}: manifest license does not match source registry")
    if declared_license_url != normalized.license_url:
        raise ValueError(f"{normalized.name}: manifest license_url does not match source registry")
    if declared_evidence_sha256 != normalized.license_evidence_sha256:
        raise ValueError(
            f"{normalized.name}: manifest license evidence digest does not match source registry",
        )
    if requested_mode in FULL_IMPORT_MODES:
        if normalized.manifest_sha256 is None:
            raise LicenseGateError(
                f"{normalized.name}: full-body import requires a registered immutable "
                "full-manifest binding",
            )
        actual_manifest_digest = canonical_ingestion_manifest_sha256(manifest)
        if not secrets.compare_digest(
            actual_manifest_digest,
            normalized.manifest_sha256,
        ):
            raise LicenseGateError(
                f"{normalized.name}: full-manifest binding mismatch; registered="
                f"{normalized.manifest_sha256}, actual={actual_manifest_digest}",
            )
    return validate_import_plan(replace(normalized, import_mode=requested_mode))


def entity_provenance(record: ExternalSourceRecord) -> dict[str, Any]:
    """Return canonical entity frontmatter fields for a validated source record."""

    normalized = validate_import_plan(record)
    return {
        "source": normalized.name,
        "source_url": normalized.url,
        "source_revision": normalized.revision,
        "license": normalized.license,
        "license_url": normalized.license_url,
        "import_mode": normalized.import_mode,
        "permission_status": normalized.permission_status,
        "permission_reference": normalized.permission_reference,
        "permission_evidence_sha256": normalized.permission_evidence_sha256,
        "license_evidence_sha256": normalized.license_evidence_sha256,
        "license_evidence_path": normalized.license_evidence_path,
        "manifest_sha256": normalized.manifest_sha256,
        "redistribution_obligations": list(normalized.redistribution_obligations),
        "notice_reference": normalized.notice_reference,
    }


def render_third_party_notice(records: Iterable[ExternalSourceRecord]) -> str:
    """Render deterministic provenance evidence; this is not a legal conclusion."""

    validated = validate_source_registry(records)
    lines = [
        "# Third-Party Source Notice",
        "",
        "Generated from `ctx.core.source_registry.BUILTIN_EXTERNAL_SOURCES`.",
        "",
        "This is a fail-closed engineering provenance inventory, not legal advice.",
        "A declared license, allowlist decision, or permission record does not prove",
        "ownership, license validity, compatibility, redistribution rights, or compliance.",
        "The controls below are minimum engineering safeguards, not exhaustive legal",
        "obligations. Existing unresolved corpus remains blocked pending evidence and review.",
        "",
        "| Source | Import mode | Permission status | Permission evidence | "
        "Declared license | License evidence | Manifest SHA-256 | "
        "Redistribution controls | Notice reference | Revision | Notes |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in validated:
        license_text = _markdown_cell(record.license)
        if record.license_url:
            license_text = _markdown_link(record.license, record.license_url)
        permission_evidence = record.permission_reference or "not recorded"
        if record.permission_evidence_sha256:
            permission_evidence += f" (sha256: {record.permission_evidence_sha256})"
        license_evidence = record.license_evidence_path or "not recorded"
        if record.license_evidence_sha256:
            license_evidence += f" (sha256: {record.license_evidence_sha256})"
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_link(record.name, record.url),
                    _markdown_cell(record.import_mode),
                    _markdown_cell(record.permission_status),
                    _markdown_cell(permission_evidence),
                    license_text,
                    _markdown_cell(license_evidence),
                    _markdown_cell(record.manifest_sha256 or "not registered"),
                    _markdown_cell(_render_obligations(record.redistribution_obligations)),
                    (
                        _markdown_link("notice", record.notice_reference)
                        if record.notice_reference
                        and record.notice_reference.startswith("https://")
                        else _markdown_cell(record.notice_reference or "not recorded")
                    ),
                    _markdown_cell(record.revision),
                    _markdown_cell(record.notes or ""),
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _markdown_cell(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", r"\\")
        .replace("|", r"\|")
        .replace("[", r"\[")
        .replace("]", r"\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _markdown_link(label: str, url: str) -> str:
    safe_url = url
    for character, escaped in MARKDOWN_UNSAFE_URL_CHARACTERS.items():
        safe_url = safe_url.replace(character, escaped)
    return f"[{_markdown_cell(label)}](<{safe_url}>)"


def _render_obligations(obligations: tuple[str, ...]) -> str:
    return " ".join(
        REDISTRIBUTION_OBLIGATION_LABELS.get(obligation, obligation) for obligation in obligations
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
    return list(validate_source_registry(records))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Validate ctx external source import policy.")
    parser.add_argument("--registry", type=Path, help="Optional JSON registry path.")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Emit records as JSON.")
    output.add_argument("--notice", action="store_true", help="Emit the generated notice.")
    args = parser.parse_args(argv)

    records = (
        load_source_registry(args.registry) if args.registry else list(BUILTIN_EXTERNAL_SOURCES)
    )
    records = list(validate_source_registry(records))

    if args.json:
        print(json.dumps({"sources": [record.to_dict() for record in records]}, indent=2))
    elif args.notice:
        print(render_third_party_notice(records), end="")
    else:
        print(f"Validated {len(records)} external source record(s).")


if __name__ == "__main__":
    main()
