"""Enforce that product/CI contract changes include test changes."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

RELEASE_METADATA_FILES = {
    "CHANGELOG.md",
    "pyproject.toml",
    "src/__init__.py",
    "src/ctx/__init__.py",
}
RELEASE_GENERATED_STATS_FILES = {
    "README.md",
    "docs/index.md",
    "docs/knowledge-graph.md",
}
MAINTAINER_SCRIPT_CONTRACT_FILES = {
    "scripts/clean_host_contract.py",
    "scripts/graph_artifact_guard.py",
    "scripts/pack_full_wiki_tar.py",
    "scripts/sync_huggingface.py",
}
GATE_CONFIG_CONTRACT_FILES = {
    ".github/dependabot.yml",
    ".github/requirements-no-test-policy.txt",
    ".no-mistakes.yaml",
    "scripts/local_fast_gate.py",
    "scripts/no_mistakes_run.sh",
}
DEPENDABOT_ACTOR = "dependabot[bot]"
PYTHON_DEPENDENCY_FILE_RE = re.compile(
    r"(?:requirements|constraints)(?:[-_.][A-Za-z0-9_.-]+)?\.txt"
)
ACTION_USES_RE = re.compile(
    r"(?P<action>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)"
    r"@(?P<ref>[^\s]+)"
)
ACTION_SEMVER_REF_RE = re.compile(r"(?P<prefix>v?)(?P<version>\d+(?:\.\d+){0,2})")
ACTION_SHA_REF_RE = re.compile(r"[0-9a-fA-F]{40}")
DEPENDENCY_LIST_SENTINEL = ("__ctx_dependency_list__",)
ACTION_USES_SENTINEL = "__ctx_action_uses__"
SUPPORTED_VERSION_OPERATORS = frozenset({"===", "==", "~=", "<=", ">=", "<", ">"})
VERSION_LINE_RE = re.compile(r'version = "\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]*)?"')
INIT_VERSION_LINE_RE = re.compile(r'__version__ = "\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]*)?"')
TEST_COUNT_STATS_RE = re.compile(
    r".*(Tests-\d+_(?:collected|inventory)|[\d,]+ (?:tests collected|test inventory)).*"
)
RELEASE_DOCS_LINE_RE = re.compile(r"\*\*v\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]*)?\*\*.*")
KNOWLEDGE_GRAPH_STATS_LINE_RE = re.compile(
    r"(?:"
    r"\| (?:Total nodes|Curated core nodes|Body-backed skill nodes|Total edges|"
    r"Hydrated skill incident edges|Hydrated skill semantic incident edges|"
    r"Edge sources \(overlap-deduped\)|Cross-type edges \(skill <-> agent\)|"
    r"Cross-type edges \(skill <-> MCP\)|Cross-type edges \(agent <-> MCP\)|"
    r"Harness edges|Shipped skill index) \| .+ \|"
    r"|is \*\*[\d,]+ nodes\*\* \([\d,]+ curated skills \+ [\d,]+ agents "
    r"\+ [\d,]+ MCP servers \+ [\d,]+ harnesses\).*"
    r"|tarball also carries \*\*[\d,]+ skill pages\*\*; \*\*[\d,]+\*\*.*"
    r"|# [\d,]+ nodes, [\d,]+ edges"
    r"|The shipped artifact currently records \*\*[\d,]+ nodes\*\*, "
    r"\*\*[\d,]+ edges\*\*,.*"
    r")"
)


class _WorkflowLoader(yaml.SafeLoader):
    def construct_mapping(
        self,
        node: yaml.MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unhashable key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


_WorkflowLoader.yaml_implicit_resolvers = {
    key: [resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:bool"]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


@dataclass(frozen=True)
class PolicyResult:
    passed: bool
    message: str
    contract_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()


def is_contract_file(path: str) -> bool:
    return (
        (path.startswith("src/") and path.endswith((".py", ".json")))
        or path.startswith("scripts/ci_")
        or path in MAINTAINER_SCRIPT_CONTRACT_FILES
        or path in GATE_CONFIG_CONTRACT_FILES
        or path == "pyproject.toml"
        or is_python_dependency_file(path)
        or path.startswith(".github/actions/")
        or (path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")))
    ) and not path.startswith("src/tests/")


def is_test_file(path: str) -> bool:
    return path.startswith("src/tests/") and path.endswith((".py", ".json"))


def _content_diff_lines(diff_text: str) -> tuple[str, ...]:
    return tuple(
        line
        for line in diff_text.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def is_python_dependency_file(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    filename = normalized.rsplit("/", maxsplit=1)[-1]
    return normalized == "pyproject.toml" or bool(PYTHON_DEPENDENCY_FILE_RE.fullmatch(filename))


def _raw_marker(spec: str, requirement: Requirement) -> str | None:
    if requirement.marker is None:
        return ""
    quote = ""
    escaped = False
    for index, character in enumerate(spec):
        if escaped:
            escaped = False
        elif character == "\\" and quote:
            escaped = True
        elif quote:
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == ";":
            return spec[index:]
    return None


def _requirement_parts(
    spec: str,
) -> tuple[Requirement, tuple[str, tuple[str, ...], str, str]] | None:
    try:
        requirement = Requirement(spec)
    except InvalidRequirement:
        return None
    marker = _raw_marker(spec, requirement)
    if marker is None:
        return None
    identity = (
        canonicalize_name(requirement.name),
        tuple(sorted(canonicalize_name(extra) for extra in requirement.extras)),
        marker,
        requirement.url or "",
    )
    return requirement, identity


def _version_constraints(requirement: Requirement) -> dict[str, Version] | None:
    constraints: dict[str, Version] = {}
    for specifier in requirement.specifier:
        if specifier.operator not in SUPPORTED_VERSION_OPERATORS:
            return None
        if specifier.operator in constraints:
            return None
        try:
            constraints[specifier.operator] = Version(specifier.version)
        except InvalidVersion:
            return None
    return constraints


def _compatible_upper_bound(version: Version) -> Version | None:
    release = version.release
    if len(release) < 2:
        return None
    prefix = list(release[:-1])
    prefix[-1] += 1
    value = ".".join(str(component) for component in prefix)
    return Version(f"{version.epoch}!{value}" if version.epoch else value)


def _has_satisfiable_version(
    requirement: Requirement,
    constraints: Mapping[str, Version],
) -> bool:
    exact = {version for operator, version in constraints.items() if operator in {"==", "==="}}
    if exact:
        return len(exact) == 1 and requirement.specifier.contains(
            next(iter(exact)),
            prereleases=True,
        )

    lower: tuple[Version, bool] | None = None
    upper: tuple[Version, bool] | None = None
    for operator, version in constraints.items():
        if operator in {">", ">=", "~="}:
            candidate = (version, operator != ">")
            if lower is None or candidate[0] > lower[0]:
                lower = candidate
            elif candidate[0] == lower[0]:
                lower = (lower[0], lower[1] and candidate[1])
        if operator in {"<", "<="}:
            candidate = (version, operator == "<=")
            if upper is None or candidate[0] < upper[0]:
                upper = candidate
            elif candidate[0] == upper[0]:
                upper = (upper[0], upper[1] and candidate[1])
        if operator == "~=":
            compatible_upper = _compatible_upper_bound(version)
            if compatible_upper is None:
                return False
            candidate = (compatible_upper, False)
            if upper is None or candidate[0] < upper[0]:
                upper = candidate
            elif candidate[0] == upper[0]:
                upper = (upper[0], upper[1] and candidate[1])

    if lower is None or upper is None or lower[0] < upper[0]:
        return True
    if lower[0] > upper[0] or not lower[1] or not upper[1]:
        return False
    return requirement.specifier.contains(lower[0], prereleases=True)


def _is_forward_requirement_update(before: str, after: str) -> bool:
    old_parts = _requirement_parts(before)
    new_parts = _requirement_parts(after)
    if old_parts is None or new_parts is None or old_parts[1] != new_parts[1]:
        return False
    old_requirement, identity = old_parts
    new_requirement = new_parts[0]
    if identity[3]:
        return False
    old_constraints = _version_constraints(old_requirement)
    new_constraints = _version_constraints(new_requirement)
    if (
        not old_constraints
        or new_constraints is None
        or old_constraints.keys() != new_constraints.keys()
        or not _has_satisfiable_version(old_requirement, old_constraints)
        or not _has_satisfiable_version(new_requirement, new_constraints)
    ):
        return False
    return all(
        new_constraints[operator] >= version for operator, version in old_constraints.items()
    ) and any(new_constraints[operator] > version for operator, version in old_constraints.items())


def _capture_dependency_list(
    container: dict[str, Any],
    key: str,
    location: tuple[str, ...],
    groups: dict[tuple[str, ...], tuple[str, ...]],
) -> bool:
    if key not in container:
        return True
    value = container[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False
    groups[location] = tuple(value)
    container[key] = DEPENDENCY_LIST_SENTINEL
    return True


def _pyproject_snapshot(
    text: str,
) -> tuple[dict[str, Any], dict[tuple[str, ...], tuple[str, ...]]] | None:
    try:
        structure = copy.deepcopy(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    groups: dict[tuple[str, ...], tuple[str, ...]] = {}

    project = structure.get("project")
    if project is not None:
        if not isinstance(project, dict):
            return None
        if not _capture_dependency_list(
            project,
            "dependencies",
            ("project", "dependencies"),
            groups,
        ):
            return None
        optional = project.get("optional-dependencies")
        if optional is not None:
            if not isinstance(optional, dict):
                return None
            for group_name in tuple(optional):
                if not isinstance(group_name, str) or not _capture_dependency_list(
                    optional,
                    group_name,
                    ("project", "optional-dependencies", group_name),
                    groups,
                ):
                    return None

    build_system = structure.get("build-system")
    if build_system is not None:
        if not isinstance(build_system, dict) or not _capture_dependency_list(
            build_system,
            "requires",
            ("build-system", "requires"),
            groups,
        ):
            return None
    return structure, groups


def _is_pyproject_dependency_update(before: str, after: str) -> bool:
    old_snapshot = _pyproject_snapshot(before)
    new_snapshot = _pyproject_snapshot(after)
    if old_snapshot is None or new_snapshot is None:
        return False
    old_structure, old_groups = old_snapshot
    new_structure, new_groups = new_snapshot
    if old_structure != new_structure or old_groups.keys() != new_groups.keys():
        return False

    changed = False
    for location, old_requirements in old_groups.items():
        new_requirements = new_groups[location]
        if len(old_requirements) != len(new_requirements):
            return False
        for old_requirement, new_requirement in zip(old_requirements, new_requirements):
            if old_requirement == new_requirement:
                continue
            if not _is_forward_requirement_update(old_requirement, new_requirement):
                return False
            changed = True
    return changed


def _split_line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\r", "\n")):
        return line[:-1], line[-1:]
    return line, ""


def _requirements_line_parts(line: str) -> tuple[tuple[str, ...], str | None] | None:
    content, ending = _split_line_ending(line)
    stripped = content.strip()
    if not stripped or stripped.startswith("#"):
        return ("literal", content, ending), None
    if stripped.startswith("-"):
        return None

    comment_match = re.search(r"\s+#", content)
    comment = ""
    declaration = content
    if comment_match is not None:
        declaration = content[: comment_match.start()]
        comment = content[comment_match.start() :]
    wrapper_match = re.fullmatch(
        r"(?P<leading>\s*)(?P<spec>\S(?:.*\S)?)(?P<trailing>\s*)",
        declaration,
    )
    if wrapper_match is None:
        return None
    spec = wrapper_match.group("spec")
    parts = _requirement_parts(spec)
    if parts is None or parts[0].url is not None:
        return None
    wrapper = (
        "requirement",
        wrapper_match.group("leading"),
        wrapper_match.group("trailing"),
        comment,
        ending,
    )
    return wrapper, spec


def _requirements_records(
    text: str,
) -> tuple[tuple[tuple[tuple[str, ...], ...], str | None, tuple[str, ...]], ...] | None:
    lines = text.splitlines(keepends=True)
    records: list[tuple[tuple[tuple[str, ...], ...], str | None, tuple[str, ...]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        parts = _requirements_line_parts(line)
        if parts is not None:
            records.append(((parts[0],), parts[1], ()))
            index += 1
            continue

        content, ending = _split_line_ending(line)
        continuation_match = re.fullmatch(
            r"(?P<declaration>.*\S)(?P<before>[ \t]+)\\(?P<after>[ \t]*)",
            content,
        )
        if continuation_match is None:
            return None
        declaration_parts = _requirements_line_parts(
            continuation_match.group("declaration") + ending
        )
        if declaration_parts is None or declaration_parts[1] is None or declaration_parts[0][3]:
            return None

        wrappers = [
            (
                *declaration_parts[0],
                continuation_match.group("before"),
                "\\",
                continuation_match.group("after"),
            )
        ]
        digests: list[str] = []
        has_next_hash = True
        index += 1
        while has_next_hash:
            if index >= len(lines):
                return None
            hash_content, hash_ending = _split_line_ending(lines[index])
            hash_match = re.fullmatch(
                r"(?P<leading>[ \t]*)--hash=sha256:"
                r"(?P<digest>[0-9a-f]{64})"
                r"(?:(?P<before>[ \t]+)(?P<continuation>\\)"
                r"(?P<after>[ \t]*)|(?P<trailing>[ \t]*))",
                hash_content,
            )
            if hash_match is None:
                return None
            has_next_hash = hash_match.group("continuation") == "\\"
            wrappers.append(
                (
                    "sha256",
                    hash_match.group("leading"),
                    hash_match.group("before") or "",
                    hash_match.group("continuation") or "",
                    hash_match.group("after") or "",
                    hash_match.group("trailing") or "",
                    hash_ending,
                )
            )
            digests.append(hash_match.group("digest"))
            index += 1
        if len(set(digests)) != len(digests):
            return None
        records.append((tuple(wrappers), declaration_parts[1], tuple(digests)))
    return tuple(records)


def _is_requirements_dependency_update(before: str, after: str) -> bool:
    old_records = _requirements_records(before)
    new_records = _requirements_records(after)
    if old_records is None or new_records is None or len(old_records) != len(new_records):
        return False

    changed = False
    for old_record, new_record in zip(old_records, new_records):
        if old_record == new_record:
            continue
        if (
            old_record[1] is None
            or new_record[1] is None
            or old_record[0][0] != new_record[0][0]
            or not _is_forward_requirement_update(old_record[1], new_record[1])
            or bool(old_record[2]) != bool(new_record[2])
            or (old_record[2] and frozenset(old_record[2]) == frozenset(new_record[2]))
        ):
            return False
        changed = True
    return changed


def _action_uses_paths(
    document: Any,
    path: str,
) -> dict[tuple[str | int, ...], str] | None:
    if not isinstance(document, dict):
        return None
    references: dict[tuple[str | int, ...], str] = {}
    step_groups: list[tuple[tuple[str | int, ...], Any]] = []
    if path.startswith(".github/workflows/"):
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            return None
        for job_name, job in jobs.items():
            if not isinstance(job_name, str) or not isinstance(job, dict):
                return None
            if "steps" in job:
                step_groups.append((("jobs", job_name, "steps"), job["steps"]))
    elif path.startswith(".github/actions/"):
        runs = document.get("runs")
        if not isinstance(runs, dict):
            return None
        if "steps" in runs:
            step_groups.append((("runs", "steps"), runs["steps"]))
    else:
        return None

    for prefix, steps in step_groups:
        if not isinstance(steps, list):
            return None
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                return None
            if "uses" not in step:
                continue
            uses = step["uses"]
            if not isinstance(uses, str):
                return None
            references[(*prefix, index, "uses")] = uses
    return references


def _replace_yaml_paths(document: Any, paths: Iterable[tuple[str | int, ...]]) -> Any:
    structure = copy.deepcopy(document)
    for path in paths:
        container = structure
        for component in path[:-1]:
            container = container[component]
        container[path[-1]] = ACTION_USES_SENTINEL
    return structure


def _yaml_action_snapshot(
    text: str,
    path: str,
) -> tuple[Any, dict[tuple[str | int, ...], str]] | None:
    try:
        document = yaml.load(text, Loader=_WorkflowLoader)
    except yaml.YAMLError:
        return None
    references = _action_uses_paths(document, path)
    if references is None:
        return None
    return _replace_yaml_paths(document, references), references


def _action_parts(value: str) -> tuple[str, str] | None:
    match = ACTION_USES_RE.fullmatch(value)
    if match is None:
        return None
    return match.group("action"), match.group("ref")


def _is_forward_action_ref(old_ref: str, new_ref: str) -> bool:
    old_tag = ACTION_SEMVER_REF_RE.fullmatch(old_ref)
    new_tag = ACTION_SEMVER_REF_RE.fullmatch(new_ref)
    if old_tag is not None and new_tag is not None:
        old_text_parts = old_tag.group("version").split(".")
        new_text_parts = new_tag.group("version").split(".")
        if any(
            len(part) > 1 and part.startswith("0") for part in (*old_text_parts, *new_text_parts)
        ):
            return False
        old_parts = tuple(int(part) for part in old_text_parts)
        new_parts = tuple(int(part) for part in new_text_parts)
        return (
            old_tag.group("prefix") == new_tag.group("prefix")
            and len(old_parts) == len(new_parts)
            and new_parts > old_parts
        )
    return (
        ACTION_SHA_REF_RE.fullmatch(old_ref) is not None
        and ACTION_SHA_REF_RE.fullmatch(new_ref) is not None
        and old_ref.lower() != new_ref.lower()
    )


def _is_github_action_ref_update(path: str, before: str, after: str) -> bool:
    if not path.endswith((".yml", ".yaml")):
        return False
    old_snapshot = _yaml_action_snapshot(before, path)
    new_snapshot = _yaml_action_snapshot(after, path)
    if old_snapshot is None or new_snapshot is None:
        return False
    old_structure, old_references = old_snapshot
    new_structure, new_references = new_snapshot
    if old_structure != new_structure or old_references.keys() != new_references.keys():
        return False

    changed = False
    for location, old_value in old_references.items():
        new_value = new_references[location]
        if old_value == new_value:
            continue
        old_parts = _action_parts(old_value)
        new_parts = _action_parts(new_value)
        if (
            old_parts is None
            or new_parts is None
            or old_parts[0] != new_parts[0]
            or not _is_forward_action_ref(old_parts[1], new_parts[1])
        ):
            return False
        changed = True
    return changed


def is_dependabot_dependency_only(
    changed_files: Iterable[str],
    blobs_by_file: Mapping[str, tuple[str, str]] | None,
) -> bool:
    files = tuple(path.strip().replace("\\", "/") for path in changed_files if path)
    if not files or blobs_by_file is None:
        return False
    for path in files:
        blobs = blobs_by_file.get(path)
        if blobs is None:
            return False
        before, after = blobs
        if path == "pyproject.toml" and _is_pyproject_dependency_update(before, after):
            continue
        if path != "pyproject.toml" and is_python_dependency_file(path):
            if _is_requirements_dependency_update(before, after):
                continue
        if _is_github_action_ref_update(path, before, after):
            continue
        return False
    return True


def is_release_metadata_only(
    changed_files: Iterable[str],
    diffs_by_file: dict[str, str],
) -> bool:
    files = tuple(path.strip().replace("\\", "/") for path in changed_files if path)
    allowed_files = RELEASE_METADATA_FILES | RELEASE_GENERATED_STATS_FILES
    if not files or any(path not in allowed_files for path in files):
        return False
    if not any(path in RELEASE_METADATA_FILES for path in files):
        return False

    for path in files:
        if path == "CHANGELOG.md":
            continue
        change_lines = _content_diff_lines(diffs_by_file.get(path, ""))
        if not change_lines:
            return False
        if path in RELEASE_GENERATED_STATS_FILES:
            for line in change_lines:
                text = line[1:].strip()
                if (
                    not TEST_COUNT_STATS_RE.fullmatch(text)
                    and not (path == "docs/index.md" and RELEASE_DOCS_LINE_RE.fullmatch(text))
                    and not (
                        path == "docs/knowledge-graph.md"
                        and KNOWLEDGE_GRAPH_STATS_LINE_RE.fullmatch(text)
                    )
                ):
                    return False
            continue
        expected = VERSION_LINE_RE if path == "pyproject.toml" else INIT_VERSION_LINE_RE
        for line in change_lines:
            if not expected.fullmatch(line[1:].strip()):
                return False
    return True


def evaluate_policy(
    changed_files: Iterable[str],
    labels: Iterable[str],
    diffs_by_file: dict[str, str],
    actor: str = "",
    blobs_by_file: Mapping[str, tuple[str, str]] | None = None,
) -> PolicyResult:
    files = tuple(path.strip().replace("\\", "/") for path in changed_files if path)
    contract = tuple(path for path in files if is_contract_file(path))
    tests = tuple(path for path in files if is_test_file(path))
    if not contract:
        return PolicyResult(True, "No product or CI/package contract changes.")
    if actor == DEPENDABOT_ACTOR:
        if is_dependabot_dependency_only(files, blobs_by_file):
            return PolicyResult(
                True,
                "Policy exempted for Dependabot dependency version updates.",
                contract,
            )
        return PolicyResult(
            False,
            "Dependabot changed files beyond dependency version updates.",
            contract,
            tests,
        )
    if tests:
        return PolicyResult(True, "Policy satisfied.", contract, tests)
    if "no-tests-needed" in set(labels):
        return PolicyResult(True, "Policy exempted by no-tests-needed label.", contract)
    if is_release_metadata_only(files, diffs_by_file):
        return PolicyResult(True, "Policy exempted for release metadata-only changes.", contract)
    return PolicyResult(False, "Contract files changed without accompanying tests.", contract)


def _git_lines(*args: str) -> tuple[str, ...]:
    proc = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return tuple(line.strip() for line in proc.stdout.splitlines() if line.strip())


def _git_text(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def _changed_files(base: str, head: str) -> tuple[str, ...]:
    return _git_lines("diff", "--name-only", base, head)


def _diffs_by_file(base: str, head: str, files: Iterable[str]) -> dict[str, str]:
    return {path: _git_text("diff", "--unified=0", base, head, "--", path) for path in files}


def _git_blob(revision: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _blobs_by_file(
    base: str,
    head: str,
    files: Iterable[str],
) -> dict[str, tuple[str, str]]:
    blobs: dict[str, tuple[str, str]] = {}
    for path in files:
        before = _git_blob(base, path)
        after = _git_blob(head, path)
        if before is not None and after is not None:
            blobs[path] = (before, after)
    return blobs


def _parse_labels(raw: str) -> tuple[str, ...]:
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(labels, list):
        return ()
    return tuple(label for label in labels if isinstance(label, str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--labels-json", default="[]")
    parser.add_argument("--actor", default="")
    args = parser.parse_args(argv)

    files = _changed_files(args.base, args.head)
    blobs = _blobs_by_file(args.base, args.head, files) if args.actor == DEPENDABOT_ACTOR else None
    result = evaluate_policy(
        files,
        _parse_labels(args.labels_json),
        _diffs_by_file(args.base, args.head, files),
        actor=args.actor,
        blobs_by_file=blobs,
    )
    print(result.message)
    if result.contract_files:
        print("Contract files:")
        print("\n".join(result.contract_files))
    if result.test_files:
        print("Test files:")
        print("\n".join(result.test_files))
    if not result.passed:
        print("::error::Policy violation - contract files changed but no tests changed.")
        print("Fix: add/update tests, or use release metadata-only changes.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
