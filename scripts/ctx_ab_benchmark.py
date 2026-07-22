#!/usr/bin/env python3
"""Run reproducible feature-development trials with and without ctx context."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS = ROOT / "benchmarks" / "ctx_ab" / "scenarios.yaml"
ORIGINAL_CODEX_HOME = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
INCIDENT_FIELDS = (
    "timestamp",
    "scenario",
    "arm",
    "attempt",
    "stage",
    "severity",
    "status",
    "message",
    "evidence",
)
PROCESS_MARKER = "CTX_BENCHMARK_PROCESS_TOKEN"
TREATMENT_ARMS = ("baseline", "ctx-light", "ctx-full")
ARM_PERMUTATIONS = (
    ("baseline", "ctx-light", "ctx-full"),
    ("baseline", "ctx-full", "ctx-light"),
    ("ctx-light", "baseline", "ctx-full"),
    ("ctx-light", "ctx-full", "baseline"),
    ("ctx-full", "baseline", "ctx-light"),
    ("ctx-full", "ctx-light", "baseline"),
)


@dataclass(frozen=True)
class Scenario:
    id: str
    repo_url: str
    commit: str
    task: str
    query: str
    language: str
    benchmark_class: str
    test_path: str
    test_body: str
    verify: tuple[str, ...]
    expected_test_count: int
    regression_verify: tuple[tuple[str, ...], ...]
    red_failure_contains: str
    reference_patch: str
    allowed_changes: tuple[str, ...]
    context: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    elapsed: float
    timed_out: bool = False
    reaped_descendants: int = 0
    residual_descendants: tuple[int, ...] = ()


def _safe_relative_path(value: object, *, field: str) -> str:
    raw = str(value or "").strip()
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts or raw != path.as_posix():
        raise ValueError(f"{field} must be a normalized relative POSIX path: {raw!r}")
    return raw


def _validated_command(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a non-empty string list")
    return tuple(value)


class IncidentLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=INCIDENT_FIELDS).writeheader()

    def add(
        self,
        *,
        scenario: str,
        arm: str,
        attempt: int,
        stage: str,
        message: str,
        evidence: str,
        severity: str = "error",
        status: str = "open",
    ) -> None:
        row = {
            "timestamp": datetime.now(UTC).isoformat(),
            "scenario": scenario,
            "arm": arm,
            "attempt": attempt,
            "stage": stage,
            "severity": severity,
            "status": status,
            "message": message,
            "evidence": evidence,
        }
        with self.path.open("a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=INCIDENT_FIELDS).writerow(row)

    def resolve_attempts(
        self,
        *,
        scenario: str,
        arm: str,
        attempts: set[int],
        resolved_by: int,
    ) -> int:
        with self.path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        resolved = 0
        for row in rows:
            try:
                attempt = int(row["attempt"])
            except (KeyError, ValueError):
                continue
            if (
                row.get("scenario") != scenario
                or row.get("arm") != arm
                or row.get("status") != "open"
                or attempt not in attempts
            ):
                continue
            row["status"] = "resolved"
            row["evidence"] = (
                f"{row.get('evidence', '').rstrip()}; recovered by attempt {resolved_by}"
            ).lstrip("; ")
            resolved += 1
        if resolved:
            with self.path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=INCIDENT_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
        return resolved

    def unresolved_count(self) -> int:
        with self.path.open(newline="", encoding="utf-8") as fh:
            return sum(row.get("status") == "open" for row in csv.DictReader(fh))


def load_scenarios(path: Path) -> list[Scenario]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = raw.get("scenarios") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("scenarios.yaml must contain a non-empty scenarios list")
    scenarios: list[Scenario] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each scenario must be an object")
        scenario_id = str(row.get("id") or "").strip()
        if not SAFE_NAME.fullmatch(scenario_id) or scenario_id in seen:
            raise ValueError(f"invalid or duplicate scenario id: {scenario_id!r}")
        seen.add(scenario_id)
        context = row.get("ctx_context")
        verify = row.get("verify")
        regression_verify = row.get("regression_verify")
        allowed_changes = row.get("allowed_changes")
        if not isinstance(context, list) or not isinstance(regression_verify, list):
            raise ValueError(f"{scenario_id}: ctx_context and verify must be lists")
        if not isinstance(allowed_changes, list) or not allowed_changes:
            raise ValueError(f"{scenario_id}: allowed_changes must be a non-empty list")
        expected_test_count = row.get("expected_test_count")
        if not isinstance(expected_test_count, int) or expected_test_count < 1:
            raise ValueError(f"{scenario_id}: expected_test_count must be a positive integer")
        commit = str(row["commit"])
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"{scenario_id}: commit must be a full lowercase SHA-1")
        repo_url = str(row["repo_url"])
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", repo_url):
            raise ValueError(f"{scenario_id}: repo_url must be an HTTPS GitHub .git URL")
        benchmark_class = str(row.get("benchmark_class") or "").strip()
        if benchmark_class not in {"trivial", "escalation"}:
            raise ValueError(f"{scenario_id}: benchmark_class must be 'trivial' or 'escalation'")
        validated_context: list[dict[str, Any]] = []
        for item in context:
            if not isinstance(item, dict) or item.get("type") not in {
                "skill",
                "agent",
                "mcp-server",
            }:
                raise ValueError(f"{scenario_id}: invalid ctx_context entry")
            slug = str(item.get("slug") or "")
            if not SAFE_NAME.fullmatch(slug):
                raise ValueError(f"{scenario_id}: invalid context slug: {slug!r}")
            validated_context.append(dict(item))
        reference_patch = str(row.get("reference_patch") or "")
        red_failure_contains = str(row.get("red_failure_contains") or "").strip()
        if not reference_patch.strip() or "../" in reference_patch:
            raise ValueError(f"{scenario_id}: reference_patch is missing or unsafe")
        if not red_failure_contains:
            raise ValueError(f"{scenario_id}: red_failure_contains must be non-empty")
        scenarios.append(
            Scenario(
                id=scenario_id,
                repo_url=repo_url,
                commit=commit,
                task=str(row["task"]).strip(),
                query=str(row["query"]).strip(),
                language=str(row.get("language") or "python"),
                benchmark_class=benchmark_class,
                test_path=_safe_relative_path(row["test_path"], field=f"{scenario_id}.test_path"),
                test_body=str(row["test_body"]),
                verify=_validated_command(verify, field=f"{scenario_id}.verify"),
                expected_test_count=expected_test_count,
                regression_verify=tuple(
                    _validated_command(command, field=f"{scenario_id}.regression_verify")
                    for command in regression_verify
                ),
                red_failure_contains=red_failure_contains,
                reference_patch=reference_patch,
                allowed_changes=tuple(
                    _safe_relative_path(path, field=f"{scenario_id}.allowed_changes")
                    for path in allowed_changes
                ),
                context=tuple(validated_context),
            )
        )
    return scenarios


def _descendant_pids(root_pid: int) -> list[int]:
    if os.name == "nt":
        return []
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,ppid="],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in result.stdout.splitlines():
        try:
            pid_text, parent_text = line.split()
            children.setdefault(int(parent_text), []).append(int(pid_text))
        except (ValueError, TypeError):
            continue
    found: list[int] = []
    pending = [root_pid]
    while pending:
        current = pending.pop()
        direct = children.get(current, [])
        found.extend(direct)
        pending.extend(direct)
    return found


def _signal_process_tree(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    descendants = _descendant_pids(process.pid)
    if os.name != "nt":
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    else:
        (process.kill if sig == getattr(signal, "SIGKILL", signal.SIGTERM) else process.terminate)()
    for pid in reversed(descendants):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _terminate_process_tree(process: subprocess.Popen[str]) -> tuple[str, str]:
    _signal_process_tree(process, signal.SIGTERM)
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired as first:
        _signal_process_tree(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            return process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            stdout = first.stdout if isinstance(first.stdout, str) else ""
            stderr = first.stderr if isinstance(first.stderr, str) else ""
            return stdout, stderr + "\nprocess tree did not reap within 12 seconds"


def _marked_process_pids(token: str) -> tuple[set[int], str | None]:
    if os.name == "nt":
        return set(), "marked descendant verification is unavailable on Windows"
    try:
        result = subprocess.run(
            ["/bin/ps", "eww", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), f"process marker scan failed: {exc}"
    if result.returncode:
        return set(), f"process marker scan exited {result.returncode}"
    marker = f"{PROCESS_MARKER}={token}"
    pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2 or marker not in fields[1]:
            continue
        try:
            pid = int(fields[0])
        except ValueError:
            continue
        if pid != os.getpid():
            pids.add(pid)
    return pids, None


def _cleanup_marked_processes(token: str) -> tuple[int, tuple[int, ...], str | None]:
    pids, error = _marked_process_pids(token)
    if error:
        return 0, (), error
    signaled = set(pids)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + 2
    while pids and time.monotonic() < deadline:
        time.sleep(0.05)
        pids, error = _marked_process_pids(token)
        if error:
            return len(signaled), (), error
        signaled.update(pids)
    for pid in pids:
        try:
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except (ProcessLookupError, PermissionError):
            pass
    if pids:
        time.sleep(0.05)
    residual, error = _marked_process_pids(token)
    return len(signaled), tuple(sorted(residual)), error


def _verification_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: float = 600,
    input_text: str | None = None,
    resource_limits: bool = False,
    contain_descendants: bool = False,
) -> CommandResult:
    started = time.perf_counter()
    process_token = secrets.token_hex(16) if contain_descendants else None
    child_env = env
    if process_token is not None:
        child_env = dict(os.environ if env is None else env)
        child_env[PROCESS_MARKER] = process_token
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=child_env,
        text=True,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name != "nt"),
        preexec_fn=_verification_limits if resource_limits and os.name != "nt" else None,
    )
    timed_out = False
    reaped = 0
    residual: tuple[int, ...] = ()
    cleanup_error: str | None = None

    def cleanup_marked_processes() -> None:
        nonlocal reaped, residual, cleanup_error
        if process_token is None:
            return
        count, remaining, error = _cleanup_marked_processes(process_token)
        reaped += count
        residual = remaining
        cleanup_error = cleanup_error or error

    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        cleanup_marked_processes()
        stdout, stderr = _terminate_process_tree(process)
    except BaseException:
        cleanup_marked_processes()
        _terminate_process_tree(process)
        raise
    finally:
        cleanup_marked_processes()
    returncode = process.returncode or (124 if timed_out else 0)
    if cleanup_error or residual:
        returncode = returncode or 125
        detail = cleanup_error or f"residual descendants: {list(residual)}"
        stderr = f"{stderr}\nbenchmark process containment failed: {detail}".lstrip()
    return CommandResult(
        returncode,
        stdout,
        stderr,
        time.perf_counter() - started,
        timed_out=timed_out,
        reaped_descendants=reaped,
        residual_descendants=residual,
    )


def ensure_repo_cache(scenario: Scenario, cache_root: Path) -> Path:
    cache = cache_root / scenario.id
    cache_root.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        result = run_process(
            ["git", "clone", "--mirror", scenario.repo_url, str(cache)],
            cwd=cache_root,
            timeout=300,
        )
        if result.returncode:
            raise RuntimeError(f"clone failed: {result.stderr.strip()}")
    present = run_process(
        ["git", "cat-file", "-e", f"{scenario.commit}^{{commit}}"], cwd=cache, timeout=30
    )
    if present.returncode:
        fetched = run_process(["git", "fetch", "origin", scenario.commit], cwd=cache, timeout=300)
        if fetched.returncode:
            raise RuntimeError(f"commit fetch failed: {fetched.stderr.strip()}")
    return cache


def prepare_workspace(scenario: Scenario, cache: Path, destination: Path) -> str:
    cloned = run_process(["git", "clone", str(cache), str(destination)], cwd=destination.parent)
    if cloned.returncode:
        raise RuntimeError(f"local clone failed: {cloned.stderr.strip()}")
    checked_out = run_process(
        ["git", "checkout", "--detach", scenario.commit], cwd=destination, timeout=60
    )
    if checked_out.returncode:
        raise RuntimeError(f"checkout failed: {checked_out.stderr.strip()}")
    test_path = destination / scenario.test_path
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(scenario.test_body, encoding="utf-8")
    return hashlib.sha256(test_path.read_bytes()).hexdigest()


def write_ctx_fixture(scenario: Scenario, home: Path) -> Path:
    wiki = home / ".claude" / "skill-wiki"
    nodes: list[dict[str, Any]] = []
    for item in scenario.context:
        entity_type = str(item["type"])
        slug = str(item["slug"])
        entity_id = f"{entity_type}:{slug}"
        tags = [str(tag) for tag in item.get("tags", [])]
        nodes.append({"id": entity_id, "label": slug, "type": entity_type, "tags": tags})
        plural = {"skill": "skills", "agent": "agents", "mcp-server": "mcp-servers"}[entity_type]
        page_dir = wiki / "entities" / plural
        if entity_type == "mcp-server":
            page_dir /= slug[0].lower() if slug and slug[0].isalpha() else "0-9"
        page = page_dir / f"{slug}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            "---\n"
            f"name: {slug}\n"
            f"title: {item.get('title', slug)}\n"
            f"type: {entity_type}\n"
            f"tags: [{', '.join(tags)}]\n"
            "status: active\n"
            "---\n\n"
            f"# {item.get('title', slug)}\n\n{str(item['body']).strip()}\n",
            encoding="utf-8",
        )
        if entity_type == "skill":
            body_path = wiki / "converted" / slug / "SKILL.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(str(item["body"]).strip() + "\n", encoding="utf-8")
        elif entity_type == "agent":
            body_path = wiki / "converted-agents" / f"{slug}.md"
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(str(item["body"]).strip() + "\n", encoding="utf-8")
    edges = [
        {"source": nodes[index]["id"], "target": nodes[index + 1]["id"], "weight": 0.8}
        for index in range(len(nodes) - 1)
    ]
    graph_path = wiki / "graphify-out" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(
        json.dumps(
            {
                "directed": False,
                "multigraph": False,
                "graph": {},
                "nodes": nodes,
                "edges": edges,
            }
        ),
        encoding="utf-8",
    )
    return wiki


def _ctx_env(home: Path, lifecycle_root: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in ("LANG", "LC_ALL", "LC_CTYPE", "PATH", "SSL_CERT_DIR", "SSL_CERT_FILE")
        if os.environ.get(key)
    }
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONNOUSERSITE": "1",
            "CTX_RUNTIME_LIFECYCLE_DIR": str(lifecycle_root),
            "CTX_TELEMETRY_ENABLED": "0",
            "CODEX_HOME": ORIGINAL_CODEX_HOME,
        }
    )
    return env


def recommend_context(
    scenario: Scenario, *, home: Path, lifecycle_root: Path
) -> list[dict[str, Any]]:
    base_command = [
        sys.executable,
        "-m",
        "ctx.cli.recommend",
        scenario.query,
        "--json",
        "--top-k",
        "5",
        "--local-code-task",
        "--no-api-keys",
        "--language",
        scenario.language,
        "--show-unavailable",
    ]

    def invoke(extra: list[str] | None = None) -> dict[str, Any]:
        result = run_process(
            [*base_command, *(extra or [])],
            cwd=ROOT,
            env=_ctx_env(home, lifecycle_root),
            timeout=90,
        )
        if result.returncode:
            raise RuntimeError(f"ctx recommendation failed: {result.stderr.strip()}")
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("ctx recommendation returned a non-object payload")
        return payload

    payload = invoke()
    raw_rows = payload.get("results")
    if not isinstance(raw_rows, list):
        raise RuntimeError("ctx recommendation returned no results list")
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    expected = {f"{item['type']}:{item['slug']}" for item in scenario.context}
    found = {str(row.get("id")) for row in rows}
    missing = expected - found
    if missing and rows:
        seed = next(
            (str(row.get("id")) for row in rows if str(row.get("id", "")).startswith("skill:")),
            str(rows[0].get("id")),
        )
        related_payload = invoke(["--selected", seed, "--related-top-n", "5"])
        selection = related_payload.get("selection")
        related_rows = selection.get("related_results") if isinstance(selection, dict) else []
        second_rows = related_payload.get("results")
        combined = [
            *(second_rows if isinstance(second_rows, list) else []),
            *(related_rows if isinstance(related_rows, list) else []),
        ]
        for row in combined:
            if not isinstance(row, dict) or str(row.get("id")) in found:
                continue
            rows.append(dict(row))
            found.add(str(row.get("id")))
        missing = expected - found
    if missing:
        raise RuntimeError(f"ctx recommendation omitted controlled entities: {sorted(missing)}")
    wiki = home / ".claude" / "skill-wiki"
    controlled = [row for row in rows if str(row.get("id")) in expected]
    unavailable: list[str] = []
    for row in controlled:
        entity_id = str(row.get("id"))
        try:
            source_path = _safe_relative_path(
                row.get("source_path"), field=f"{entity_id}.source_path"
            )
        except ValueError:
            unavailable.append(entity_id)
            continue
        if row.get("installable") is not True or not (wiki / source_path).is_file():
            unavailable.append(entity_id)
    if unavailable:
        raise RuntimeError(
            f"ctx recommendation returned unavailable controlled entities: {sorted(unavailable)}"
        )
    return rows


def arms_for_mode(mode: str) -> tuple[str, ...]:
    if mode == "both":
        return ("baseline", "ctx-light")
    if mode == "all":
        return TREATMENT_ARMS
    if mode not in TREATMENT_ARMS:
        raise ValueError(f"unknown benchmark arm: {mode}")
    return (mode,)


def ordered_arms(scenario_id: str, trial: int, arms: tuple[str, ...]) -> tuple[str, ...]:
    if trial < 1:
        raise ValueError("trial must be >= 1")
    digest = int(hashlib.sha256(scenario_id.encode()).hexdigest()[:8], 16)
    if set(arms) == set(TREATMENT_ARMS) and len(arms) == len(TREATMENT_ARMS):
        return ARM_PERMUTATIONS[(digest + trial - 1) % len(ARM_PERMUTATIONS)]
    if len(arms) == 2 and (digest + trial - 1) % 2:
        return tuple(reversed(arms))
    return arms


def trial_schedule(
    scenarios: list[Scenario], arms: tuple[str, ...], trials: int
) -> list[dict[str, Any]]:
    return [
        {
            "scenario": scenario.id,
            "trial": trial,
            "arms": list(ordered_arms(scenario.id, trial, arms)),
        }
        for scenario in scenarios
        for trial in range(1, trials + 1)
    ]


def treatment_policy_valid(
    treatment_level: str,
    *,
    skill_used: bool,
    mcp_used: bool,
    agent_attempted: bool,
    agent_used: bool,
) -> bool:
    if treatment_level == "baseline":
        return True
    if treatment_level == "ctx-light":
        return skill_used and not mcp_used and not agent_attempted
    if treatment_level == "ctx-full":
        return skill_used and mcp_used and agent_used
    raise ValueError(f"unsupported treatment level: {treatment_level}")


def next_treatment_level(
    arm: str,
    current: str,
    *,
    agent_returncode: object,
    agent_timed_out: object,
    policy_valid: object,
    verification_returncode: object,
) -> str:
    if (
        arm == "ctx-light"
        and current == "ctx-light"
        and agent_returncode == 0
        and agent_timed_out is False
        and policy_valid is True
        and verification_returncode == 1
    ):
        return "ctx-full"
    return current


def make_lifecycle_store(root: Path) -> Any:
    from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore  # noqa: PLC0415

    return RuntimeLifecycleStore(root=root)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def mcp_config(python: str) -> list[str]:
    return [
        "-c",
        f"mcp_servers.ctx-wiki.command={_toml_string(python)}",
        "-c",
        'mcp_servers.ctx-wiki.args=["-m","ctx.mcp_server.server"]',
        "-c",
        'mcp_servers.ctx-wiki.default_tools_approval_mode="approve"',
        "-c",
        'mcp_servers.ctx-wiki.enabled_tools=["ctx__wiki_get"]',
        "-c",
        "mcp_servers.ctx-wiki.required=true",
    ]


def preflight_ctx_mcp(
    scenario: Scenario,
    *,
    home: Path,
    lifecycle_root: Path,
    session_id: str,
) -> dict[str, Any]:
    from ctx.adapters.generic.tools import McpClient, McpServerConfig  # noqa: PLC0415

    skill = next(item for item in scenario.context if item["type"] == "skill")
    config = McpServerConfig(
        name="ctx-wiki",
        command=sys.executable,
        args=("-m", "ctx.mcp_server.server"),
        env={
            "HOME": str(home),
            "PYTHONPATH": str(ROOT / "src"),
            "CTX_RUNTIME_LIFECYCLE_DIR": str(lifecycle_root),
            "CTX_TELEMETRY_ENABLED": "0",
        },
        startup_timeout=5.0,
        request_timeout=5.0,
    )
    client = McpClient(config, session_id=session_id)
    try:
        client.start()
        names = {tool.name for tool in client.list_tools()}
        required = {"ctx__wiki_get", "ctx__recommend_bundle"}
        if missing := required - names:
            raise RuntimeError(f"ctx MCP missing tools: {sorted(missing)}")
        raw = client.call_tool(
            "ctx__wiki_get",
            {"slug": skill["slug"], "entity_type": skill["type"]},
        )
        payload = json.loads(raw)
        if (
            payload.get("slug") != skill["slug"]
            or payload.get("entity_type") != skill["type"]
            or str(skill["body"]).strip() not in str(payload.get("body") or "")
        ):
            raise RuntimeError("ctx MCP returned the wrong benchmark fixture")
        return {
            "status": "passed",
            "tool_count": len(names),
            "probe": "ctx__wiki_get",
            "fixture": f"{skill['type']}:{skill['slug']}",
            "response_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
    finally:
        client.stop()


def task_prompt(scenario: Scenario) -> str:
    verify_argv = [part.replace("{python}", sys.executable) for part in scenario.verify]
    verify = f"PYTHONPATH=src {shlex.join(verify_argv)}"
    return (
        "Implement the feature below in this repository.\n\n"
        f"TASK\n{scenario.task}\n\n"
        "REQUIRED LOOP\n"
        "1. Plan: inspect the relevant code and state a short implementation plan.\n"
        "2. Code: implement the smallest complete change that follows repository conventions.\n"
        "3. Test: run the focused verification command and fix failures.\n"
        "4. Check: review the final diff and run git diff --check.\n\n"
        f"FOCUSED VERIFICATION\n{verify}\n\n"
        f"The evaluator owns {scenario.test_path}; do not edit or delete it. "
        "Do not modify any test, test configuration, or import configuration; the "
        "provided PYTHONPATH already selects this clone's source. "
        "Finish only when the focused verification and diff check pass."
    )


def context_prompt(scenario: Scenario, treatment_level: str) -> str:
    skill = next(item for item in scenario.context if item["type"] == "skill")
    if treatment_level == "ctx-light":
        return (
            "\n\nCTX SELECTED CONTEXT\n"
            f"[SKILL {skill['slug']}]\n{str(skill['body']).strip()}\n\n"
            "Use this selected local skill when relevant. ctx did not select an MCP "
            "or delegated reviewer for this small task; do not add either one."
        )
    if treatment_level != "ctx-full":
        raise ValueError(f"unsupported ctx treatment: {treatment_level}")
    reviewer = next(item for item in scenario.context if item["type"] == "agent")
    marker = f"CTX_REVIEWER:{reviewer['slug']}"
    return (
        "\n\nCTX FULL TREATMENT\n"
        "The ctx-wiki MCP is active for this explicitly selected full treatment. "
        "During Plan, call "
        f"ctx__wiki_get for skill {skill['slug']!r} with entity_type='skill'. "
        "Use the returned skill body instead of searching for another workflow. "
        "After coding and focused tests, use spawn_agent once to delegate this exact "
        "bounded review, then wait once for completion. Begin the spawn_agent prompt "
        f"with exactly {marker!r}, followed by:\n"
        f"[AGENT {reviewer['slug']}]\n{str(reviewer['body']).strip()}\n"
        "Give the reviewer the current working directory, changed-file list, and "
        "focused verification command. Address actionable findings, close the "
        "reviewer without repeating its report, then finish git diff --check."
    )


def _jsonl_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def extract_token_usage(stdout: str) -> dict[str, Any]:
    terminal: list[tuple[int, dict[str, Any]]] = []
    for index, event in enumerate(_jsonl_events(stdout)):
        usage = event.get("usage")
        if event.get("type") == "turn.completed" and isinstance(usage, dict):
            terminal.append((index, usage))
    if not terminal:
        return {"attribution": "unavailable", "reason": "Codex JSONL exposed no usage"}
    event_index, usage = terminal[-1]
    required = ("input_tokens", "cached_input_tokens", "output_tokens")
    if not all(isinstance(usage.get(key), int) and usage[key] >= 0 for key in required):
        return {
            "attribution": "unavailable",
            "reason": "terminal turn.completed usage was incomplete",
        }
    normalized = {key: int(usage[key]) for key in required}
    if normalized["cached_input_tokens"] > normalized["input_tokens"]:
        return {
            "attribution": "unavailable",
            "reason": "terminal turn.completed cached input exceeded total input",
        }
    normalized["uncached_input_tokens"] = (
        normalized["input_tokens"] - normalized["cached_input_tokens"]
    )
    for key in ("cache_write_input_tokens", "reasoning_output_tokens"):
        if isinstance(usage.get(key), int) and usage[key] >= 0:
            normalized[key] = int(usage[key])
    normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    return {
        "attribution": "exact",
        "attribution_source": "terminal turn.completed.usage",
        "usage_event_index": event_index,
        **normalized,
    }


def _mcp_result_payload(result: object) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    structured = result.get("structured_content")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("text"), str):
            continue
        try:
            payload = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def observed_mcp_tool_use(stdout: str, *, slug: str, entity_type: str, expected_body: str) -> bool:
    for event in _jsonl_events(stdout):
        item = event.get("item")
        arguments = item.get("arguments") if isinstance(item, dict) else None
        payload = _mcp_result_payload(item.get("result")) if isinstance(item, dict) else None
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "mcp_tool_call"
            and item.get("server") == "ctx-wiki"
            and item.get("tool") == "ctx__wiki_get"
            and isinstance(arguments, dict)
            and arguments.get("slug") == slug
            and arguments.get("entity_type") == entity_type
            and item.get("status") == "completed"
            and item.get("error") is None
            and isinstance(payload, dict)
            and payload.get("slug") == slug
            and payload.get("entity_type") == entity_type
            and expected_body.strip() in str(payload.get("body") or "")
        ):
            return True
    return False


def required_tool_failures(stdout: str) -> list[str]:
    failures: list[str] = []
    for event in _jsonl_events(stdout):
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        item_type = item.get("type")
        tool = str(item.get("tool") or "")
        required = (
            item_type == "mcp_tool_call"
            and item.get("server") == "ctx-wiki"
            and tool == "ctx__wiki_get"
        ) or (item_type == "collab_tool_call" and tool in {"spawn_agent", "wait", "close_agent"})
        if not required or (item.get("status") == "completed" and item.get("error") is None):
            continue
        error = item.get("error")
        detail = str(error.get("message") if isinstance(error, dict) else error or "")
        failures.append(f"{item_type}:{tool} status={item.get('status')}: {detail}".rstrip())
    return failures


def observed_agent_attempt(stdout: str) -> bool:
    return any(
        isinstance(event.get("item"), dict)
        and event["item"].get("type") == "collab_tool_call"
        and event["item"].get("tool") == "spawn_agent"
        for event in _jsonl_events(stdout)
    )


def observed_agent_review(stdout: str, *, reviewer_slug: str, expected_instructions: str) -> bool:
    states: dict[str, int] = {}
    for event in _jsonl_events(stdout):
        item = event.get("item")
        if (
            event.get("type") != "item.completed"
            or not isinstance(item, dict)
            or item.get("type") != "collab_tool_call"
            or item.get("status") != "completed"
            or item.get("error") is not None
        ):
            continue
        if item.get("tool") == "spawn_agent":
            receivers = item.get("receiver_thread_ids")
            prompt = item.get("prompt")
            marker = f"CTX_REVIEWER:{reviewer_slug}"
            if (
                isinstance(receivers, list)
                and isinstance(prompt, str)
                and prompt.startswith(marker)
                and expected_instructions.strip() in prompt
            ):
                for receiver in receivers:
                    if receiver:
                        states.setdefault(str(receiver), 1)
        elif item.get("tool") == "wait":
            agent_states = item.get("agents_states")
            if isinstance(agent_states, dict):
                for agent_id, state in agent_states.items():
                    key = str(agent_id)
                    if (
                        states.get(key) == 1
                        and isinstance(state, dict)
                        and state.get("status") == "completed"
                        and isinstance(state.get("message"), str)
                        and state["message"].strip()
                    ):
                        states[key] = 2
        elif item.get("tool") == "close_agent":
            receivers = item.get("receiver_thread_ids")
            if isinstance(receivers, list):
                for receiver in receivers:
                    key = str(receiver)
                    if states.get(key) == 2:
                        states[key] = 3
    return 3 in states.values()


def observed_model_turn(stdout: str) -> bool:
    return any(
        event.get("type") in {"turn.started", "turn.completed"} for event in _jsonl_events(stdout)
    )


def close_context_session(
    store: Any,
    selected_items: list[dict[str, Any]],
    *,
    session_id: str,
    model: str,
    status: str,
    usage_evidence: dict[str, str],
) -> None:
    for item in selected_items:
        entity_type = str(item["type"])
        if evidence := usage_evidence.get(entity_type):
            store.mark_entity_used(
                session_id=session_id,
                entity_type=entity_type,
                slug=str(item["slug"]),
                evidence=evidence,
                token_usage={
                    "attribution": "unavailable",
                    "attribution_reason": "Codex reports session tokens, not per-context tokens",
                    "model": model,
                },
            )
        store.unload_entity(
            session_id=session_id,
            entity_type=entity_type,
            slug=str(item["slug"]),
            reason="ephemeral benchmark process ended",
        )
    store.end_session(
        session_id=session_id,
        status=status,
        summary="A/B benchmark arm completed",
    )


def codex_command(
    *, codex: str, model: str, workspace: Path, prompt: str, with_ctx: bool
) -> list[str]:
    command = [
        codex,
        "-a",
        "never",
        "--enable" if with_ctx else "--disable",
        "multi_agent",
    ]
    if with_ctx:
        command.extend(mcp_config(sys.executable))
    command.extend(
        [
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--model",
            model,
            "--sandbox",
            "workspace-write",
            "--cd",
            str(workspace),
            prompt,
        ]
    )
    return command


def _verification_env(workspace: Path, temp: Path) -> dict[str, str]:
    return {
        "CODEX_HOME": ORIGINAL_CODEX_HOME,
        "HOME": str(temp),
        "TMPDIR": str(temp),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "PYTHONPATH": str(workspace / "src"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def _run_verified(argv: list[str], *, workspace: Path, timeout: float = 180) -> CommandResult:
    codex = shutil.which("codex")
    if sys.platform != "darwin" or codex is None:
        raise RuntimeError("live verification requires the Codex-managed macOS sandbox")
    temp = workspace.parent / "verification-tmp"
    temp.mkdir(parents=True, exist_ok=True)
    return run_process(
        [
            codex,
            "sandbox",
            "-P",
            ":workspace",
            "--sandbox-state-disable-network",
            "-C",
            str(workspace),
            "--",
            *argv,
        ],
        cwd=workspace,
        env=_verification_env(workspace, temp),
        timeout=timeout,
        resource_limits=True,
        contain_descendants=True,
    )


def _pytest_pass_count(output: str) -> int | None:
    matches = re.findall(r"(?:^|\s)(\d+) passed(?:[\s,]|$)", output)
    return int(matches[-1]) if matches else None


def _focused_verification(scenario: Scenario, workspace: Path, test_hash: str) -> CommandResult:
    test_path = workspace / scenario.test_path
    if not test_path.is_file() or hashlib.sha256(test_path.read_bytes()).hexdigest() != test_hash:
        return CommandResult(1, "", "benchmark-owned test was changed", 0.0)
    argv = [part.replace("{python}", sys.executable) for part in scenario.verify]
    focused = _run_verified(argv, workspace=workspace)
    if focused.returncode:
        return focused
    count = _pytest_pass_count(focused.stdout + focused.stderr)
    if count != scenario.expected_test_count:
        return CommandResult(
            1,
            focused.stdout,
            focused.stderr
            + f"\nexpected {scenario.expected_test_count} focused tests, observed {count}",
            focused.elapsed,
        )
    return focused


def _materialize_untracked_changes(scenario: Scenario, workspace: Path) -> CommandResult:
    started = time.perf_counter()
    indexed_test = run_process(
        ["git", "ls-files", "--error-unmatch", scenario.test_path], cwd=workspace, timeout=30
    )
    if not indexed_test.returncode:
        return CommandResult(
            1,
            "",
            "benchmark-owned test entered the git index",
            time.perf_counter() - started,
        )
    untracked = run_process(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=workspace,
        timeout=30,
    )
    if untracked.returncode:
        return CommandResult(
            untracked.returncode,
            untracked.stdout,
            untracked.stderr,
            time.perf_counter() - started,
        )
    paths = [path for path in untracked.stdout.split("\0") if path and path != scenario.test_path]
    if paths:
        added = run_process(["git", "add", "-N", "--", *paths], cwd=workspace, timeout=30)
        if added.returncode:
            return CommandResult(
                added.returncode,
                added.stdout,
                added.stderr,
                time.perf_counter() - started,
            )
    changed = run_process(["git", "diff", "--name-only", "-z", "HEAD"], cwd=workspace, timeout=30)
    if changed.returncode:
        return CommandResult(
            changed.returncode,
            changed.stdout,
            changed.stderr,
            time.perf_counter() - started,
        )
    changed_paths = {path for path in changed.stdout.split("\0") if path}
    disallowed = changed_paths - set(scenario.allowed_changes)
    if disallowed:
        return CommandResult(
            1,
            changed.stdout,
            f"changes outside scenario allowlist: {sorted(disallowed)}",
            time.perf_counter() - started,
        )
    return CommandResult(0, changed.stdout, "", time.perf_counter() - started)


def verify_workspace(scenario: Scenario, workspace: Path, test_hash: str) -> CommandResult:
    focused = _focused_verification(scenario, workspace, test_hash)
    if focused.returncode:
        return focused
    stdout = focused.stdout
    stderr = focused.stderr
    elapsed = focused.elapsed
    for command in scenario.regression_verify:
        argv = [part.replace("{python}", sys.executable) for part in command]
        regression = _run_verified(argv, workspace=workspace)
        stdout += regression.stdout
        stderr += regression.stderr
        elapsed += regression.elapsed
        if regression.returncode:
            return CommandResult(regression.returncode, stdout, stderr, elapsed)
    materialized = _materialize_untracked_changes(scenario, workspace)
    if materialized.returncode:
        return CommandResult(
            1,
            stdout + materialized.stdout,
            stderr + materialized.stderr,
            elapsed + materialized.elapsed,
        )
    elapsed += materialized.elapsed
    diff_check = run_process(["git", "diff", "--check", "HEAD"], cwd=workspace, timeout=30)
    return CommandResult(
        diff_check.returncode,
        stdout + diff_check.stdout,
        stderr + diff_check.stderr,
        elapsed + diff_check.elapsed,
    )


def validate_evaluator_controls(
    scenario: Scenario,
    *,
    cache: Path,
    output: Path,
) -> dict[str, Any]:
    controls = output / scenario.id / "controls"
    red_workspace = controls / "red" / "repo"
    red_workspace.parent.mkdir(parents=True, exist_ok=True)
    red_hash = prepare_workspace(scenario, cache, red_workspace)
    red = _focused_verification(scenario, red_workspace, red_hash)
    (controls / "red.log").write_text(
        f"returncode={red.returncode}\n{red.stdout}{red.stderr}", encoding="utf-8"
    )
    if red.returncode in {70, 71} and "sandbox" in red.stderr.lower():
        raise RuntimeError("verification sandbox could not be applied")
    if not red.returncode or scenario.red_failure_contains not in red.stdout + red.stderr:
        raise RuntimeError(
            "evaluator red control did not fail for the expected missing feature: "
            f"{scenario.red_failure_contains!r}"
        )

    reference_workspace = controls / "reference" / "repo"
    reference_workspace.parent.mkdir(parents=True, exist_ok=True)
    reference_hash = prepare_workspace(scenario, cache, reference_workspace)
    applied = run_process(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=reference_workspace,
        input_text=scenario.reference_patch,
        timeout=30,
    )
    if applied.returncode:
        raise RuntimeError(f"reference patch failed: {applied.stderr.strip()}")
    reference = verify_workspace(scenario, reference_workspace, reference_hash)
    (controls / "reference.log").write_text(reference.stdout + reference.stderr, encoding="utf-8")
    if reference.returncode:
        raise RuntimeError(f"evaluator reference control failed: {reference.stderr.strip()}")
    result = {
        "status": "passed",
        "red_failure_observed": scenario.red_failure_contains,
        "red_seconds": round(red.elapsed, 6),
        "reference_seconds": round(reference.elapsed, 6),
        "expected_focused_tests": scenario.expected_test_count,
        "regression_commands": [list(command) for command in scenario.regression_verify],
        "reference_patch_sha256": hashlib.sha256(scenario.reference_patch.encode()).hexdigest(),
    }
    (controls / "control.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def _command_version(argv: list[str]) -> str:
    result = run_process(argv, cwd=ROOT, timeout=30)
    return (result.stdout or result.stderr).strip() if not result.returncode else "unavailable"


def write_environment_manifest(
    *,
    output: Path,
    scenarios_path: Path,
    scenarios: list[Scenario],
    codex: str,
    model: str,
    run_config: dict[str, Any],
    schedule: list[dict[str, Any]],
) -> None:
    revision = run_process(["git", "rev-parse", "HEAD"], cwd=ROOT, timeout=30)
    repository_status = run_process(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=ROOT, timeout=30
    )
    tracked_diff = run_process(["git", "diff", "--binary", "HEAD"], cwd=ROOT, timeout=30)
    dependencies = run_process(
        [sys.executable, "-m", "pip", "freeze", "--all"], cwd=ROOT, timeout=60
    )
    scenario_bytes = scenarios_path.read_bytes()
    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": sys.version,
        "python_executable": sys.executable,
        "codex_binary": codex,
        "codex_version": _command_version([codex, "--version"]),
        "model": model,
        "ctx_revision": revision.stdout.strip() if not revision.returncode else "unavailable",
        "repository_state": {
            "clean": not repository_status.returncode and not repository_status.stdout.strip(),
            "status": repository_status.stdout.splitlines(),
            "tracked_diff_sha256": (
                hashlib.sha256(tracked_diff.stdout.encode()).hexdigest()
                if not tracked_diff.returncode
                else "unavailable"
            ),
        },
        "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scenarios_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
        "scenario_ids": [scenario.id for scenario in scenarios],
        "scenario_classes": {scenario.id: scenario.benchmark_class for scenario in scenarios},
        "run_config": run_config,
        "schedule": schedule,
        "dependency_freeze": dependencies.stdout.splitlines()
        if not dependencies.returncode
        else [],
        "verification": {
            "sandbox": "Codex-managed macOS :workspace profile",
            "network": "denied",
            "environment": "allowlist",
            "resource_limits": ["cpu", "file-size", "open-files"],
        },
        "codex_environment_keys": sorted(
            _ctx_env(output / "manifest-home", output / "manifest-lifecycle")
        ),
        "token_scope": "terminal Codex turn; per-subagent and per-context attribution unavailable",
    }
    (output / "environment.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def run_trial(
    scenario: Scenario,
    *,
    arm: str,
    treatment_level: str,
    attempt: int,
    trial: int,
    retry: int,
    cache: Path,
    output: Path,
    codex: str,
    model: str,
    timeout: float,
    dry_run: bool,
    incidents: IncidentLog,
) -> dict[str, Any]:
    trial_started = time.perf_counter()
    run_dir = output / scenario.id / arm / f"attempt-{attempt}"
    workspace = run_dir / "repo"
    run_dir.mkdir(parents=True, exist_ok=True)
    test_hash = prepare_workspace(scenario, cache, workspace)
    home = run_dir / "home"
    lifecycle_root = run_dir / "lifecycle"
    env = _ctx_env(home, lifecycle_root)
    base_prompt = task_prompt(scenario)
    recommendations: list[dict[str, Any]] = []
    recommended_ids: list[str] = []
    selected_ids: list[str] = []
    selected_items: list[dict[str, Any]] = []
    ctx_setup_seconds = 0.0
    ctx_enabled = treatment_level != "baseline"
    full_treatment = treatment_level == "ctx-full"
    if treatment_level not in TREATMENT_ARMS:
        raise ValueError(f"unsupported treatment level: {treatment_level}")
    store = make_lifecycle_store(lifecycle_root) if ctx_enabled else None
    session_id = f"ctx-ab-{scenario.id}-{attempt}"
    usage_evidence: dict[str, str] = {}
    session_status = "failed"
    session_closed = False
    teardown_seconds = 0.0

    def close_session() -> None:
        nonlocal session_closed, teardown_seconds
        if not ctx_enabled or store is None or session_closed:
            return
        session_closed = True
        teardown_started = time.perf_counter()
        close_context_session(
            store,
            selected_items,
            session_id=session_id,
            status=session_status,
            model=model,
            usage_evidence=usage_evidence,
        )
        teardown_seconds = time.perf_counter() - teardown_started

    try:
        if ctx_enabled:
            assert store is not None
            setup_started = time.perf_counter()
            write_ctx_fixture(scenario, home)
            recommendations = recommend_context(scenario, home=home, lifecycle_root=lifecycle_root)
            configured = {f"{item['type']}:{item['slug']}": item for item in scenario.context}
            recommended_ids = [
                str(row.get("id")) for row in recommendations if row.get("id") in configured
            ]
            if full_treatment:
                selected_items = [dict(item) for item in scenario.context]
            else:
                selected_skill_id = next(
                    entity_id
                    for entity_id in recommended_ids
                    if configured[entity_id]["type"] == "skill"
                )
                selected_items = [dict(configured[selected_skill_id])]
            selected_ids = [f"{item['type']}:{item['slug']}" for item in selected_items]
            (run_dir / "recommendations.json").write_text(
                json.dumps(
                    {
                        "query": scenario.query,
                        "treatment_level": treatment_level,
                        "recommended_ids": recommended_ids,
                        "selected_ids": selected_ids,
                        "recommendations": recommendations,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            for item in selected_items:
                store.load_entity(
                    session_id=session_id,
                    entity_type=str(item["type"]),
                    slug=str(item["slug"]),
                    reason=f"selected by explicit {treatment_level} benchmark policy",
                    selected=True,
                    selection_source="system",
                    source_context={
                        "benchmark": scenario.id,
                        "arm": arm,
                        "treatment_level": treatment_level,
                    },
                )
            if full_treatment:
                preflight = preflight_ctx_mcp(
                    scenario,
                    home=home,
                    lifecycle_root=lifecycle_root,
                    session_id=session_id,
                )
                (run_dir / "mcp-preflight.json").write_text(
                    json.dumps(preflight, indent=2) + "\n", encoding="utf-8"
                )
            base_prompt += context_prompt(scenario, treatment_level)
            ctx_setup_seconds = time.perf_counter() - setup_started
        prompt_hash = hashlib.sha256(task_prompt(scenario).encode()).hexdigest()
        treatment_hash = hashlib.sha256(base_prompt.encode()).hexdigest()
        (run_dir / "prompt.txt").write_text(base_prompt, encoding="utf-8")
        if dry_run:
            session_status = "preflight"
            close_session()
            return {
                "scenario": scenario.id,
                "arm": arm,
                "trial": trial,
                "retry": retry,
                "attempt": attempt,
                "treatment_level": treatment_level,
                "status": "dry_run",
                "verification_passed": None,
                "task_prompt_sha256": prompt_hash,
                "delivered_prompt_sha256": treatment_hash,
                "recommended_ids": recommended_ids,
                "selected_ids": selected_ids,
                "used_ids": [],
                "ctx_setup_seconds": round(ctx_setup_seconds, 6),
                "teardown_seconds": round(teardown_seconds, 6),
                "total_seconds": round(time.perf_counter() - trial_started, 6),
                "token_attribution": "unavailable",
                "artifact_dir": str(run_dir),
            }
        command = codex_command(
            codex=codex,
            model=model,
            workspace=workspace,
            prompt=base_prompt,
            with_ctx=full_treatment,
        )
        (run_dir / "command.json").write_text(
            json.dumps(
                {"argv_without_prompt": command[:-1], "prompt_sha256": treatment_hash}, indent=2
            )
            + "\n",
            encoding="utf-8",
        )
        agent = run_process(
            command,
            cwd=workspace,
            env=env,
            timeout=timeout,
            contain_descendants=True,
        )
        (run_dir / "codex.jsonl").write_text(agent.stdout, encoding="utf-8")
        (run_dir / "codex.stderr.log").write_text(agent.stderr, encoding="utf-8")
        pre_status = run_process(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            timeout=30,
        )
        (run_dir / "post-agent-status.txt").write_text(pre_status.stdout, encoding="utf-8")
        verification = verify_workspace(scenario, workspace, test_hash)
        (run_dir / "verification.log").write_text(
            verification.stdout + verification.stderr, encoding="utf-8"
        )
        status = run_process(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            timeout=30,
        )
        (run_dir / "git-status.txt").write_text(status.stdout, encoding="utf-8")
        diff = run_process(["git", "diff", "--binary", "HEAD"], cwd=workspace, timeout=30)
        (run_dir / "changes.patch").write_text(diff.stdout, encoding="utf-8")
        usage = extract_token_usage(agent.stdout)
        skill = next(item for item in scenario.context if item["type"] == "skill")
        mcp_used = observed_mcp_tool_use(
            agent.stdout,
            slug=str(skill["slug"]),
            entity_type=str(skill["type"]),
            expected_body=str(skill["body"]),
        )
        reviewer = next(item for item in scenario.context if item["type"] == "agent")
        agent_attempted = observed_agent_attempt(agent.stdout)
        agent_used = observed_agent_review(
            agent.stdout,
            reviewer_slug=str(reviewer["slug"]),
            expected_instructions=str(reviewer["body"]),
        )
        skill_used = (
            mcp_used if full_treatment else ctx_enabled and observed_model_turn(agent.stdout)
        )
        used_ids: list[str] = []
        if ctx_enabled:
            if full_treatment and skill_used:
                usage_evidence["skill"] = "selected skill body returned by runtime ctx MCP call"
                usage_evidence["mcp-server"] = (
                    "Codex JSONL recorded successful ctx-wiki ctx__wiki_get completion"
                )
                used_ids.extend(
                    [
                        f"skill:{skill['slug']}",
                        next(
                            entity_id
                            for entity_id in selected_ids
                            if entity_id.startswith("mcp-server:")
                        ),
                    ]
                )
            elif skill_used:
                usage_evidence["skill"] = "selected skill body supplied in treatment prompt"
                used_ids.append(f"skill:{skill['slug']}")
            if full_treatment and agent_used:
                usage_evidence["agent"] = (
                    "Codex JSONL recorded matching spawn, completed wait, and close events"
                )
                used_ids.append(
                    next(entity_id for entity_id in selected_ids if entity_id.startswith("agent:"))
                )
        policy_valid = treatment_policy_valid(
            treatment_level,
            skill_used=skill_used,
            mcp_used=mcp_used,
            agent_attempted=agent_attempted,
            agent_used=agent_used,
        )
        tool_failures = required_tool_failures(agent.stdout) if full_treatment else []
        for failure in tool_failures:
            incidents.add(
                scenario=scenario.id,
                arm=arm,
                attempt=attempt,
                stage="required-tool",
                message="required tool attempt failed before recovery",
                evidence=failure,
                severity="warning" if policy_valid else "error",
                status="resolved" if policy_valid else "open",
            )
        passed = not agent.returncode and not verification.returncode and policy_valid
        session_status = "passed" if passed else "failed"
        if not passed:
            reasons = []
            if agent.timed_out:
                reasons.append("Codex timed out")
            elif agent.returncode:
                reasons.append(f"agent={agent.returncode}")
            if verification.returncode:
                reasons.append(f"verification={verification.returncode}")
            if full_treatment and not mcp_used:
                reasons.append("successful runtime MCP call absent")
            if full_treatment and not agent_used:
                reasons.append("completed delegated reviewer loop absent")
            if treatment_level == "ctx-light" and (mcp_used or agent_attempted):
                reasons.append("ctx-light used an unselected expensive tool")
            if ctx_enabled and not skill_used:
                reasons.append("selected skill use was not observed")
            incidents.add(
                scenario=scenario.id,
                arm=arm,
                attempt=attempt,
                stage="live-trial",
                message="benchmark arm failed",
                evidence="; ".join(reasons),
            )
        close_session()
        return {
            "scenario": scenario.id,
            "arm": arm,
            "trial": trial,
            "retry": retry,
            "attempt": attempt,
            "benchmark_class": scenario.benchmark_class,
            "treatment_level": treatment_level,
            "escalated": treatment_level != arm,
            "first_attempt": retry == 0,
            "status": "passed" if passed else "failed",
            "verification_passed": not verification.returncode,
            "verification_returncode": verification.returncode,
            "agent_returncode": agent.returncode,
            "agent_timed_out": agent.timed_out,
            "task_prompt_sha256": prompt_hash,
            "delivered_prompt_sha256": treatment_hash,
            "recommended_ids": recommended_ids,
            "selected_ids": selected_ids,
            "used_ids": used_ids,
            "policy_valid": policy_valid,
            "ctx_setup_seconds": round(ctx_setup_seconds, 6),
            "agent_seconds": round(agent.elapsed, 6),
            "verification_seconds": round(verification.elapsed, 6),
            "teardown_seconds": round(teardown_seconds, 6),
            "measured_phase_seconds": round(
                ctx_setup_seconds + agent.elapsed + verification.elapsed + teardown_seconds,
                6,
            ),
            "total_seconds": round(time.perf_counter() - trial_started, 6),
            "token_attribution": usage.pop("attribution"),
            "token_scope": "terminal_codex_turn",
            "team_token_completeness": "unknown" if agent_attempted else "not_applicable",
            "skill_use_observed": skill_used if ctx_enabled else None,
            "mcp_tool_use_observed": mcp_used if ctx_enabled else None,
            "review_agent_use_observed": agent_used if ctx_enabled else None,
            "review_agent_attempt_observed": agent_attempted if ctx_enabled else None,
            "required_tool_failures": tool_failures,
            "reaped_descendants": agent.reaped_descendants,
            "residual_descendants": list(agent.residual_descendants),
            **usage,
            "artifact_dir": str(run_dir),
            "lifecycle_events": str(lifecycle_root / "events.jsonl") if ctx_enabled else None,
        }
    finally:
        close_session()


def write_summary(output: Path, results: list[dict[str, Any]]) -> None:
    (output / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    fields = sorted({key for row in results for key in row})
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in results:
        key = (str(row.get("scenario")), str(row.get("arm")), int(row.get("trial", 0)))
        grouped.setdefault(key, []).append(row)
    aggregate: list[dict[str, Any]] = []
    for (scenario, arm, trial), attempts in sorted(grouped.items()):
        exact_tokens = [
            int(row["total_tokens"])
            for row in attempts
            if row.get("token_attribution") == "exact" and isinstance(row.get("total_tokens"), int)
        ]
        aggregate.append(
            {
                "scenario": scenario,
                "arm": arm,
                "trial": trial,
                "attempts": len(attempts),
                "first_attempt_passed": attempts[0].get("status") == "passed",
                "eventual_passed": any(row.get("status") == "passed" for row in attempts),
                "retries_used": max(0, len(attempts) - 1),
                "cumulative_seconds": round(
                    sum(float(row.get("total_seconds", 0.0)) for row in attempts), 6
                ),
                "cumulative_exact_tokens": (
                    sum(exact_tokens) if len(exact_tokens) == len(attempts) else None
                ),
            }
        )
    (output / "aggregate.json").write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    if aggregate:
        with (output / "aggregate.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(aggregate[0]))
            writer.writeheader()
            writer.writerows(aggregate)


def build_performance_report(
    results: list[dict[str, Any]],
    *,
    scenario_ids: list[str],
    trials: int,
    arms: tuple[str, ...],
) -> dict[str, Any]:
    attempts: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for row in results:
        key = (str(row.get("scenario")), str(row.get("arm")), int(row.get("trial", 0)))
        attempts.setdefault(key, []).append(row)
    pairs: list[dict[str, Any]] = []
    for scenario_id in scenario_ids:
        for trial in range(1, trials + 1):
            baseline = attempts.get((scenario_id, "baseline", trial), [])
            light = attempts.get((scenario_id, "ctx-light", trial), [])
            pair: dict[str, Any] = {"scenario": scenario_id, "trial": trial}
            if not baseline or not light:
                pair.update({"complete": False, "reason": "paired arms missing"})
                pairs.append(pair)
                continue
            baseline_seconds = sum(float(row.get("total_seconds", 0.0)) for row in baseline)
            light_seconds = sum(float(row.get("total_seconds", 0.0)) for row in light)
            baseline_tokens = [
                int(row["total_tokens"])
                for row in baseline
                if row.get("token_attribution") == "exact"
                and isinstance(row.get("total_tokens"), int)
            ]
            light_tokens = [
                int(row["total_tokens"])
                for row in light
                if row.get("token_attribution") == "exact"
                and isinstance(row.get("total_tokens"), int)
            ]
            complete = (
                baseline[-1].get("status") == "passed"
                and light[-1].get("status") == "passed"
                and baseline_seconds > 0
                and light_seconds > 0
                and len(baseline_tokens) == len(baseline)
                and len(light_tokens) == len(light)
                and sum(baseline_tokens) > 0
                and sum(light_tokens) > 0
                and all(
                    row.get("team_token_completeness") != "unknown" for row in [*baseline, *light]
                )
            )
            if not complete:
                pair.update(
                    {
                        "complete": False,
                        "reason": "status time or token evidence missing",
                    }
                )
                pairs.append(pair)
                continue
            pair.update(
                {
                    "complete": True,
                    "baseline_first_attempt_passed": baseline[0].get("status") == "passed",
                    "ctx_light_first_attempt_passed": light[0].get("status") == "passed",
                    "time_ratio": round(light_seconds / baseline_seconds, 6),
                    "reported_token_ratio": round(sum(light_tokens) / sum(baseline_tokens), 6),
                }
            )
            pairs.append(pair)
    complete_pairs = [pair for pair in pairs if pair.get("complete")]
    expected_pairs = len(scenario_ids) * trials
    evidence_required = trials >= 6 and {"baseline", "ctx-light"}.issubset(set(arms))
    evidence_complete = len(complete_pairs) == expected_pairs
    quality_preserved = evidence_complete and all(
        not pair["baseline_first_attempt_passed"] or pair["ctx_light_first_attempt_passed"]
        for pair in complete_pairs
    )
    median_time_ratio = (
        round(median(float(pair["time_ratio"]) for pair in complete_pairs), 6)
        if complete_pairs
        else None
    )
    median_token_ratio = (
        round(
            median(float(pair["reported_token_ratio"]) for pair in complete_pairs),
            6,
        )
        if complete_pairs
        else None
    )
    gate_passed = None
    if evidence_required:
        gate_passed = bool(
            evidence_complete
            and quality_preserved
            and median_time_ratio is not None
            and median_time_ratio <= 1.10
            and median_token_ratio is not None
            and median_token_ratio <= 1.10
        )
    return {
        "status": (
            "passed" if gate_passed is True else "failed" if gate_passed is False else "diagnostic"
        ),
        "evidence_required": evidence_required,
        "evidence_complete": evidence_complete,
        "quality_preserved": quality_preserved,
        "thresholds": {
            "median_time_ratio_max": 1.10,
            "median_reported_token_ratio_max": 1.10,
        },
        "median_time_ratio": median_time_ratio,
        "median_reported_token_ratio": median_token_ratio,
        "gate_passed": gate_passed,
        "pairs": pairs,
    }


def write_performance_report(
    output: Path,
    results: list[dict[str, Any]],
    *,
    scenario_ids: list[str],
    trials: int,
    arms: tuple[str, ...],
) -> dict[str, Any]:
    report = build_performance_report(
        results,
        scenario_ids=scenario_ids,
        trials=trials,
        arms=arms,
    )
    (output / "performance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument(
        "--arm",
        choices=("baseline", "ctx-light", "ctx-full", "both", "all"),
        default="both",
    )
    parser.add_argument("--model", default=os.environ.get("CTX_BENCHMARK_MODEL", "gpt-5.5"))
    parser.add_argument("--codex", default=shutil.which("codex") or "codex")
    parser.add_argument("--cache-root", type=Path, default=Path.home() / ".cache/ctx-ab")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scenarios = load_scenarios(args.scenarios)
    if args.scenario:
        requested = set(args.scenario)
        scenarios = [scenario for scenario in scenarios if scenario.id in requested]
        missing = requested - {scenario.id for scenario in scenarios}
        if missing:
            raise SystemExit(f"unknown scenarios: {', '.join(sorted(missing))}")
    if args.list:
        for scenario in scenarios:
            print(f"{scenario.id}\t{scenario.commit}\t{scenario.repo_url}")
        return 0
    if args.trials < 1 or args.retries < 0:
        raise SystemExit("--trials must be >= 1 and --retries must be >= 0")
    if sys.platform != "darwin":
        raise SystemExit("benchmark execution currently requires macOS sandbox-exec")
    output = args.output or Path("/tmp") / f"ctx-ab-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    incidents = IncidentLog(output / "incidents.csv")
    arms = arms_for_mode(args.arm)
    schedule = trial_schedule(scenarios, arms, args.trials)
    write_environment_manifest(
        output=output,
        scenarios_path=args.scenarios,
        scenarios=scenarios,
        codex=args.codex,
        model=args.model,
        run_config={
            "arm_mode": args.arm,
            "arms": list(arms),
            "trials": args.trials,
            "retries": args.retries,
            "timeout_seconds": args.timeout,
            "dry_run": args.dry_run,
            "cache_root": str(args.cache_root),
            "scenario_filters": list(args.scenario),
        },
        schedule=schedule,
    )
    results: list[dict[str, Any]] = []
    schedule_by_key = {
        (str(row["scenario"]), int(row["trial"])): tuple(row["arms"]) for row in schedule
    }
    for scenario in scenarios:
        cache: Path | None = None
        failed_cache_attempts: set[int] = set()
        for cache_attempt in range(args.retries + 1):
            try:
                cache = ensure_repo_cache(scenario, args.cache_root)
                if failed_cache_attempts:
                    incidents.resolve_attempts(
                        scenario=scenario.id,
                        arm="control",
                        attempts=failed_cache_attempts,
                        resolved_by=cache_attempt + 1,
                    )
                break
            except Exception as exc:  # noqa: BLE001 - persist cache failures.
                failed_cache_attempts.add(cache_attempt + 1)
                incidents.add(
                    scenario=scenario.id,
                    arm="control",
                    attempt=cache_attempt + 1,
                    stage="repo-cache",
                    message=type(exc).__name__,
                    evidence=str(exc),
                )
        if cache is None:
            continue
        try:
            validate_evaluator_controls(scenario, cache=cache, output=output)
        except Exception as exc:  # noqa: BLE001 - persist control failures.
            incidents.add(
                scenario=scenario.id,
                arm="control",
                attempt=1,
                stage="evaluator-control",
                message=type(exc).__name__,
                evidence=str(exc),
            )
            continue
        for trial in range(1, args.trials + 1):
            for arm in schedule_by_key[(scenario.id, trial)]:
                treatment_level = arm
                failed_attempts: set[int] = set()
                for retry in range(args.retries + 1):
                    attempt = (trial - 1) * (args.retries + 1) + retry + 1
                    try:
                        result = run_trial(
                            scenario,
                            arm=arm,
                            treatment_level=treatment_level,
                            attempt=attempt,
                            trial=trial,
                            retry=retry,
                            cache=cache,
                            output=output,
                            codex=args.codex,
                            model=args.model,
                            timeout=args.timeout,
                            dry_run=args.dry_run,
                            incidents=incidents,
                        )
                    except Exception as exc:  # noqa: BLE001 - persist harness failures.
                        incidents.add(
                            scenario=scenario.id,
                            arm=arm,
                            attempt=attempt,
                            stage="harness",
                            message=type(exc).__name__,
                            evidence=str(exc),
                        )
                        result = {
                            "scenario": scenario.id,
                            "arm": arm,
                            "trial": trial,
                            "retry": retry,
                            "attempt": attempt,
                            "status": "harness_error",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    results.append(result)
                    write_summary(output, results)
                    if args.dry_run or result.get("status") == "passed":
                        if failed_attempts:
                            incidents.resolve_attempts(
                                scenario=scenario.id,
                                arm=arm,
                                attempts=failed_attempts,
                                resolved_by=attempt,
                            )
                        break
                    failed_attempts.add(attempt)
                    treatment_level = next_treatment_level(
                        arm,
                        treatment_level,
                        agent_returncode=result.get("agent_returncode"),
                        agent_timed_out=result.get("agent_timed_out"),
                        policy_valid=result.get("policy_valid"),
                        verification_returncode=result.get("verification_returncode"),
                    )
    expected_keys = {
        (scenario.id, arm, trial)
        for scenario in scenarios
        for arm in arms
        for trial in range(1, args.trials + 1)
    }
    if args.dry_run:
        print(output)
        observed_keys = {
            (
                str(row.get("scenario")),
                str(row.get("arm")),
                int(row.get("trial", 0)),
            )
            for row in results
        }
        return (
            0
            if observed_keys == expected_keys
            and all(row.get("status") == "dry_run" for row in results)
            else 1
        )
    performance = write_performance_report(
        output,
        results,
        scenario_ids=[scenario.id for scenario in scenarios],
        trials=args.trials,
        arms=arms,
    )
    final: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in results:
        final[
            (
                str(row.get("scenario")),
                str(row.get("arm")),
                int(row.get("trial", 0)),
            )
        ] = row
    print(output)
    return (
        0
        if set(final) == expected_keys
        and all(row.get("status") == "passed" for row in final.values())
        and performance.get("gate_passed") is not False
        and incidents.unresolved_count() == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
