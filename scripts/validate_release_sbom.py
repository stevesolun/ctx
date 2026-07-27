"""Validate the CycloneDX SBOM emitted for a ctx release wheel."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import tomllib
from typing import Any, Iterable
from urllib.parse import unquote

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


RELEASE_RUNTIME_EXTRAS = (
    "ann",
    "browser",
    "embeddings",
    "gcf",
    "harness",
    "viz",
)
_NON_RUNTIME_EXTRAS = frozenset({"dev"})
_PYPI_PURL = re.compile(r"^pkg:pypi/([^@/?#]+)@([^?#]+)(?:\?[^#]+)?(?:#.+)?$")


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be a JSON object")
    return value


def _project_metadata(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle).get("project")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read project metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("pyproject.toml must contain a [project] table")
    return value


def _identity(component: dict[str, Any], label: str) -> tuple[str, str, str]:
    raw_name = component.get("name")
    version = component.get("version")
    purl = component.get("purl")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ValueError(f"{label} must have a nonempty name")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{label} must have a nonempty version")
    if not isinstance(purl, str):
        raise ValueError(f"{label} must have a PyPI package URL")

    match = _PYPI_PURL.fullmatch(purl)
    if match is None:
        raise ValueError(f"{label} has invalid PyPI package URL: {purl!r}")
    name = canonicalize_name(raw_name)
    if canonicalize_name(unquote(match.group(1))) != name:
        raise ValueError(f"{label} package URL name does not match component name")
    if unquote(match.group(2)) != version:
        raise ValueError(f"{label} package URL version does not match component version")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"{label} has invalid version: {version!r}") from exc
    return name, version, purl


def _component_maps(
    sbom: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    raw_components = sbom.get("components")
    if not isinstance(raw_components, list):
        raise ValueError("CycloneDX SBOM must contain a components array")

    by_name: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, dict[str, Any]] = {}
    for raw in raw_components:
        if not isinstance(raw, dict):
            raise ValueError("every SBOM component must be an object")
        name, _, _ = _identity(raw, "SBOM component")
        bom_ref = raw.get("bom-ref")
        if not isinstance(bom_ref, str) or not bom_ref:
            raise ValueError(f"SBOM component {name} must have a bom-ref")
        if name in by_name:
            raise ValueError(f"duplicate SBOM component: {name}")
        if bom_ref in by_ref:
            raise ValueError(f"duplicate SBOM bom-ref: {bom_ref}")
        by_name[name] = raw
        by_ref[bom_ref] = raw
    return by_name, by_ref


def _inventory_map(inventory_path: Path) -> dict[str, dict[str, Any]]:
    inventory = _load_object(inventory_path, "resolved environment inventory")
    raw_distributions = inventory.get("distributions")
    if not isinstance(raw_distributions, list):
        raise ValueError("resolved environment inventory must contain distributions")

    distributions: dict[str, dict[str, Any]] = {}
    for raw in raw_distributions:
        if not isinstance(raw, dict):
            raise ValueError("every resolved distribution must be an object")
        raw_name = raw.get("name")
        version = raw.get("version")
        requires = raw.get("requires")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("every resolved distribution must have a name")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"resolved distribution {raw_name!r} must have a version")
        if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
            raise ValueError(f"resolved distribution {raw_name!r} must have string requirements")
        name = canonicalize_name(raw_name)
        if name in distributions:
            raise ValueError(f"duplicate resolved distribution: {name}")
        try:
            Version(version)
        except InvalidVersion as exc:
            raise ValueError(
                f"resolved distribution {name} has invalid version: {version!r}"
            ) from exc
        distributions[name] = {"name": name, "version": version, "requires": requires}
    return distributions


def _dependency_map(sbom: dict[str, Any], known_refs: set[str]) -> dict[str, set[str]]:
    raw_dependencies = sbom.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise ValueError("CycloneDX SBOM must contain a dependency graph")

    dependencies: dict[str, set[str]] = {}
    for raw in raw_dependencies:
        if not isinstance(raw, dict) or not isinstance(raw.get("ref"), str):
            raise ValueError("every dependency graph entry must have a ref")
        ref = raw["ref"]
        raw_children = raw.get("dependsOn", [])
        if not isinstance(raw_children, list) or not all(
            isinstance(child, str) for child in raw_children
        ):
            raise ValueError(f"dependency graph entry {ref!r} has invalid dependsOn")
        if ref in dependencies:
            raise ValueError(f"duplicate dependency graph ref: {ref}")
        children = set(raw_children)
        unknown = sorted(({ref} | children) - known_refs)
        if unknown:
            raise ValueError(f"dependency graph contains unknown refs: {unknown}")
        dependencies[ref] = children
    return dependencies


def _requirements(values: Iterable[object], label: str) -> list[Requirement]:
    parsed: list[Requirement] = []
    for raw in values:
        try:
            parsed.append(Requirement(str(raw)))
        except InvalidRequirement as exc:
            raise ValueError(f"{label} contains invalid requirement {raw!r}") from exc
    return parsed


def _marker_applies(requirement: Requirement, extras: set[str]) -> bool:
    if requirement.marker is None:
        return True
    environment = {key: str(value) for key, value in default_environment().items()}
    for extra in {""} | extras:
        if requirement.marker.evaluate({**environment, "extra": extra}):
            return True
    return False


def _release_requirements(
    project: dict[str, Any],
    selected_extras: tuple[str, ...],
) -> list[Requirement]:
    optional = project.get("optional-dependencies")
    if not isinstance(optional, dict):
        raise ValueError("pyproject.toml must contain project.optional-dependencies")
    expected_extras = set(optional) - _NON_RUNTIME_EXTRAS
    if set(selected_extras) != expected_extras:
        raise ValueError(
            f"release SBOM must cover every supported runtime extra: {sorted(expected_extras)}"
        )

    raw_requirements = list(project.get("dependencies", []))
    for extra in selected_extras:
        values = optional.get(extra)
        if not isinstance(values, list):
            raise ValueError(f"runtime extra {extra!r} is not declared")
        raw_requirements.extend(values)
    return _requirements(raw_requirements, "release requirements")


def _validate_component_against_inventory(
    name: str,
    component: dict[str, Any],
    inventory: dict[str, dict[str, Any]],
) -> None:
    resolved = inventory.get(name)
    if resolved is None:
        raise ValueError(f"SBOM component {name} is absent from resolved environment")
    _, version, _ = _identity(component, f"SBOM component {name}")
    if version != resolved["version"]:
        raise ValueError(
            f"SBOM component {name} version {version!r} does not match "
            f"resolved version {resolved['version']!r}"
        )


def validate_release_sbom(
    sbom_path: Path,
    pyproject_path: Path,
    inventory_path: Path,
    *,
    expected_spec: str = "1.6",
    selected_extras: tuple[str, ...] = RELEASE_RUNTIME_EXTRAS,
) -> None:
    """Verify release identity and the resolved all-extras dependency closure."""
    sbom = _load_object(sbom_path, "JSON SBOM")
    project = _project_metadata(pyproject_path)
    inventory = _inventory_map(inventory_path)

    if sbom.get("bomFormat") != "CycloneDX":
        raise ValueError("SBOM format must be CycloneDX")
    if sbom.get("specVersion") != expected_spec:
        raise ValueError(f"SBOM specVersion must be {expected_spec}")

    metadata = sbom.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    if not isinstance(root, dict):
        raise ValueError("SBOM metadata must identify the release component")
    project_name = project.get("name")
    project_version = project.get("version")
    if not isinstance(project_name, str) or not isinstance(project_version, str):
        raise ValueError("project name and version must be strings")
    root_name, root_version, _ = _identity(root, "SBOM release component")
    if root_name != canonicalize_name(project_name):
        raise ValueError("SBOM release component name does not match pyproject.toml")
    if root_version != project_version:
        raise ValueError("SBOM release component version does not match pyproject.toml")
    if root.get("type") != "application":
        raise ValueError("SBOM release component must use CycloneDX type application")
    root_ref = root.get("bom-ref")
    if not isinstance(root_ref, str) or not root_ref:
        raise ValueError("SBOM release component must have a bom-ref")
    resolved_root = inventory.get(root_name)
    if resolved_root is None or resolved_root["version"] != project_version:
        raise ValueError("resolved environment does not contain the release wheel")

    components, components_by_ref = _component_maps(sbom)
    for name, component in components.items():
        _validate_component_against_inventory(name, component, inventory)

    known_refs = set(components_by_ref) | {root_ref}
    dependency_graph = _dependency_map(sbom, known_refs)
    if root_ref not in dependency_graph:
        raise ValueError("dependency graph is missing the release root")

    selected_by_name: dict[str, set[str]] = {}
    pending: list[tuple[str, Requirement]] = [
        (root_ref, requirement)
        for requirement in _release_requirements(project, selected_extras)
        if _marker_applies(requirement, set())
    ]
    processed: set[tuple[str, tuple[str, ...]]] = set()

    while pending:
        parent_ref, requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        resolved = inventory.get(name)
        if resolved is None:
            raise ValueError(f"resolved environment is missing dependency: {name}")
        try:
            resolved_version = Version(resolved["version"])
        except InvalidVersion as exc:
            raise ValueError(f"resolved dependency {name} has invalid version") from exc
        if requirement.specifier and resolved_version not in requirement.specifier:
            raise ValueError(
                f"resolved dependency {name}=={resolved['version']} violates "
                f"declared requirement {requirement}"
            )

        runtime_component = components.get(name)
        if runtime_component is None:
            raise ValueError(f"SBOM is missing resolved runtime dependency: {name}")
        component_ref = runtime_component["bom-ref"]
        actual_children = dependency_graph.get(parent_ref)
        if actual_children is None or component_ref not in actual_children:
            raise ValueError(
                f"dependency graph is missing edge {parent_ref!r} -> {component_ref!r}"
            )

        selected_extras_for_name = selected_by_name.setdefault(name, set())
        before = set(selected_extras_for_name)
        selected_extras_for_name.update(requirement.extras)
        state = (name, tuple(sorted(selected_extras_for_name)))
        if state in processed and before == selected_extras_for_name:
            continue
        processed.add(state)

        child_requirements = _requirements(resolved["requires"], f"resolved distribution {name}")
        for child in child_requirements:
            if _marker_applies(child, selected_extras_for_name):
                pending.append((component_ref, child))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument(
        "--extra",
        action="append",
        dest="extras",
        choices=RELEASE_RUNTIME_EXTRAS,
        default=[],
    )
    parser.add_argument("--spec-version", default="1.6")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    selected_extras = tuple(args.extras) or RELEASE_RUNTIME_EXTRAS
    try:
        validate_release_sbom(
            args.sbom,
            args.pyproject,
            args.inventory,
            expected_spec=args.spec_version,
            selected_extras=selected_extras,
        )
    except ValueError as exc:
        raise SystemExit(f"invalid release SBOM: {exc}") from exc
    print(f"validated CycloneDX release SBOM against resolved all-extras environment: {args.sbom}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
