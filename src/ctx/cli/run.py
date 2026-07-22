"""ctx.cli.run — `ctx run` / `ctx resume` / `ctx sessions` CLI.

First user-facing entry to the model-agnostic harness. Ships as v1
per Plan 001 §10 success criteria:

    ctx run --provider openrouter --model minimax/minimax-m1 \\
            --mcp filesystem \\
            --task "fix the failing tests in this repo"

Three commands:
    run       - start a new agent session
    resume    - continue a prior session by id
    sessions  - list sessions + inspect a single one

Example end-to-end (Ollama, no API key needed):

    ctx run --provider ollama --model llama3.1 \\
            --task "summarize the architecture of this codebase" \\
            --mcp filesystem:/tmp/project

Plan 001 Phase H7.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shlex
import sys
import threading
import time
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ctx.adapters.generic.adaptive_runtime import AdaptiveRuntimeController, SelectedSkill
from ctx.adapters.generic.compaction import TokenBudgetCompactor
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox, make_tool_executor
from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore
from ctx.adapters.generic.loop import (
    LoopObserver,
    LoopResult,
    ToolPolicy,
    TurnActivation,
    TurnAuthorization,
    TurnPreparation,
    run_loop,
)
from ctx.adapters.generic.contract import ContractBuilder
from ctx.adapters.generic.evaluator import Evaluator, run_with_evaluation
from ctx.adapters.generic.planner import Planner, augmented_system_prompt
from ctx.adapters.generic.providers import (
    CompletionResponse,
    Message,
    ToolCall,
    ToolDefinition,
    Usage,
    get_provider,
)
from ctx.adapters.generic.state import (
    JsonlObserver,
    SessionStore,
    default_sessions_dir,
    list_sessions,
    load_session,
    new_session_id,
)
from ctx.adapters.generic.tools import TOOL_SEPARATOR, McpRouter, McpServerConfig
from ctx.telemetry import record_event, record_exception, telemetry_span
from ctx.utils._secret_scan import find_inline_secret_arg


_logger = logging.getLogger(__name__)
_CTX_SESSION_MARKER = "ctx runtime session id:"
_CTX_TOOL_INSTRUCTIONS_START = "ctx tool instructions:"
_CTX_TOOL_INSTRUCTIONS_END = "end ctx tool instructions."
_MISSING = object()
_SESSION_USAGE_ATTRIBUTION_REASON = (
    "ctx run provider usage is aggregated across the session; exact per-tool "
    "token attribution requires host-supplied ctx__mark_entity_used.token_usage."
)
_MODEL_PROFILE_NAME = "ctx-model-profile.json"
_GITHUB_MCP_CREDENTIAL_ENV = (
    "GITHUB_PERSONAL_ACCESS_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_CTX_TOOL_SURFACES = ("adaptive", "minimal", "full")
_CTX_BOOTSTRAP_TOOL_NAMES = frozenset({"ctx__recommend_bundle", "ctx__wiki_get"})
_MAX_AUXILIARY_AGENT_TIMEOUT = 45.0


class _DeferredStopObserver:
    """Forward evaluator events while persisting one orchestration-level stop."""

    def __init__(self, delegate: LoopObserver) -> None:
        self._delegate = delegate
        self._round_results: list[LoopResult] = []

    def on_iteration_start(self, iteration: int, messages: list[Message]) -> None:
        self._delegate.on_iteration_start(iteration, messages)

    def on_model_response(self, iteration: int, response: CompletionResponse) -> None:
        self._delegate.on_model_response(iteration, response)

    def on_tool_call(
        self,
        iteration: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> None:
        self._delegate.on_tool_call(iteration, call, result, error)

    def on_stop(self, result: LoopResult) -> None:
        self._round_results.append(result)

    def failure_result(self, exc: Exception) -> LoopResult:
        """Collapse completed generator rounds into one failed orchestration result."""

        previous = self._round_results[-1] if self._round_results else None
        return LoopResult(
            stop_reason="provider_error",
            final_message=previous.final_message if previous is not None else "",
            iterations=sum(result.iterations for result in self._round_results),
            usage=previous.usage if previous is not None else Usage(),
            messages=previous.messages if previous is not None else (),
            detail=f"evaluator orchestration failed: {type(exc).__name__}",
        )


# ── Provider key-env defaults ───────────────────────────────────────────────


# Tier-1 provider → env var map. The CLI reads --api-key-env or falls
# back to this table. Users can override with --api-key-env explicitly.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "huggingface": "HF_TOKEN",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "together": "TOGETHER_API_KEY",
    "groq": "GROQ_API_KEY",
    # Ollama: no key needed (local)
    "ollama": "",
}


def _model_provider_prefix(model: str) -> str:
    """Given a model string like 'openrouter/anthropic/claude-opus-4.7',
    return the leading provider segment ('openrouter')."""
    return model.split("/", 1)[0] if "/" in model else model


def _resolve_api_key_env(
    explicit: str | None,
    model: str,
    provider: str | None,
) -> str | None:
    if explicit is not None:
        return explicit if explicit else None  # empty → None (Ollama)
    prefix = provider or _model_provider_prefix(model)
    key = _PROVIDER_KEY_ENV.get(prefix)
    return key if key else None


def _claude_dir() -> Path:
    return Path(os.path.expanduser("~/.claude"))


def _load_model_profile() -> dict[str, Any]:
    path = _claude_dir() / _MODEL_PROFILE_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("ctx model profile could not be loaded: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _profile_str(profile: dict[str, Any], key: str) -> str | None:
    value = profile.get(key)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _apply_model_profile_defaults(args: argparse.Namespace) -> str | None:
    """Apply ctx-init's saved model profile to `ctx run` args."""
    profile = _load_model_profile()
    profile_model = _profile_str(profile, "model")
    if not args.model and profile_model:
        args.model = profile_model
    if not args.model:
        return (
            "error: --model is required unless "
            f"{_claude_dir() / _MODEL_PROFILE_NAME} contains a model"
        )
    if profile_model != args.model:
        return None

    if args.provider is None:
        args.provider = _profile_str(profile, "provider")
    if args.api_key_env is None and isinstance(profile.get("api_key_env"), str):
        args.api_key_env = profile["api_key_env"]
    if args.base_url is None:
        args.base_url = _profile_str(profile, "base_url")
    return None


# ── MCP spec parsing ───────────────────────────────────────────────────────


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _evaluator_rounds(raw: str) -> int:
    value = _positive_int(raw)
    if value > 2:
        raise argparse.ArgumentTypeError("must be <= 2 (one initial round plus one revision)")
    return value


def _positive_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return value


def _normalise_tool_patterns(patterns: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (patterns or []) if p and p.strip())


def _resolve_ctx_tool_surface(explicit: str | None, recorded: Any = _MISSING) -> str:
    if explicit in _CTX_TOOL_SURFACES:
        return explicit
    if recorded in _CTX_TOOL_SURFACES:
        return str(recorded)
    # Sessions created before surfaces were recorded exposed all ctx tools.
    return "full" if recorded is _MISSING else "minimal"


def _ctx_toolbox_for_surface(
    *,
    lifecycle_dir: Path | None,
    bound_session_id: str,
    surface: str,
    allow_patterns: list[str] | tuple[str, ...] | None,
    deny_patterns: list[str] | tuple[str, ...] | None,
) -> tuple[CtxCoreToolbox, list[ToolDefinition]]:
    """Build a toolbox whose submitted and executable ctx tools agree."""
    if surface not in _CTX_TOOL_SURFACES:
        raise ValueError(f"unsupported ctx tool surface: {surface!r}")
    allow = _normalise_tool_patterns(allow_patterns)
    deny = _normalise_tool_patterns(deny_patterns)
    inventory = CtxCoreToolbox(
        lifecycle_dir=lifecycle_dir,
        bound_session_id=bound_session_id,
    )
    definitions = inventory.tool_definitions()
    inventory_names = frozenset(definition.name for definition in definitions)
    if allow:
        definitions = [
            definition
            for definition in definitions
            if any(fnmatchcase(definition.name, pattern) for pattern in allow)
        ]
    elif surface == "minimal":
        definitions = [
            definition for definition in definitions if definition.name in _CTX_BOOTSTRAP_TOOL_NAMES
        ]
    elif surface == "adaptive":
        definitions = []
    if deny:
        definitions = [
            definition
            for definition in definitions
            if not any(fnmatchcase(definition.name, pattern) for pattern in deny)
        ]

    exposed_names = frozenset(definition.name for definition in definitions)
    if exposed_names == inventory_names:
        return inventory, definitions
    toolbox = CtxCoreToolbox(
        lifecycle_dir=lifecycle_dir,
        bound_session_id=bound_session_id,
        allowed_tool_names=exposed_names,
    )
    return toolbox, toolbox.tool_definitions()


def _compile_tool_policy(
    allow_patterns: list[str] | tuple[str, ...] | None,
    deny_patterns: list[str] | tuple[str, ...] | None,
) -> ToolPolicy | None:
    allow = _normalise_tool_patterns(allow_patterns)
    deny = _normalise_tool_patterns(deny_patterns)
    if not allow and not deny:
        return None

    def policy(call: ToolCall) -> str | None:
        for pattern in deny:
            if fnmatchcase(call.name, pattern):
                return f"matched deny pattern {pattern!r}"
        if allow and not any(fnmatchcase(call.name, pattern) for pattern in allow):
            return f"no allow pattern matched {call.name!r}"
        return None

    return policy


def _tool_policy_from_metadata(meta: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    raw = meta.get("tool_policy")
    if not isinstance(raw, dict):
        return (), ()
    allow = raw.get("allow") if isinstance(raw.get("allow"), list) else []
    deny = raw.get("deny") if isinstance(raw.get("deny"), list) else []
    return _normalise_tool_patterns(allow), _normalise_tool_patterns(deny)


def _resume_tool_policy_patterns(
    args: argparse.Namespace,
    meta: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    meta_allow, meta_deny = _tool_policy_from_metadata(meta)
    cli_allow = _normalise_tool_patterns(args.allow_tool)
    cli_deny = _normalise_tool_patterns(args.deny_tool)
    return (*meta_allow, *cli_allow), (*meta_deny, *cli_deny)


def _add_tool_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Allow only tool names matching this glob pattern. Repeatable. "
            "Matching ctx tools are the only ctx schemas sent to the provider. "
            "If omitted, the selected ctx tool surface is allowed unless denied."
        ),
    )
    parser.add_argument(
        "--deny-tool",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Deny tool names matching this glob pattern before execution. "
            "Denied ctx schemas are not sent to the provider. Repeatable; "
            "deny rules override allow rules."
        ),
    )


def _add_ctx_tool_surface_arg(
    parser: argparse.ArgumentParser,
    *,
    default: str | None,
) -> None:
    parser.add_argument(
        "--ctx-tool-surface",
        choices=_CTX_TOOL_SURFACES,
        default=default,
        help=(
            "CTX capability mode: 'adaptive' selects at most one installed skill "
            "from trusted user/configured roots. Explicit MCP servers stay dormant "
            "until a namespaced --allow-tool grant (or an explicit '*') selects them; "
            "--mcp alone grants all supplied servers. Their filtered schemas are "
            "leased for one turn. It "
            "abstains when secure local reads are unavailable. 'minimal' exposes "
            "recommend_bundle + wiki_get; 'full' restores the complete "
            "read/lifecycle surface. --allow-tool selects an explicit subset."
        ),
    )


def _parse_mcp_spec(spec: str) -> McpServerConfig:
    """Parse a --mcp argument.

    Two forms:
      name:<shell-invocation>
        Example: filesystem:npx -y @modelcontextprotocol/server-filesystem /data
        The part before the colon is the name; the part after is the
        command + args (split on whitespace, no shell). Secret-looking
        inline args are rejected; pass credentials through the child
        environment with --mcp-env.

      name (bare)
        Names that match a known preset get a default invocation.
        Currently recognised presets:
          filesystem → npx -y @modelcontextprotocol/server-filesystem .
          github     → npx -y @modelcontextprotocol/server-github
          git        → npx -y @modelcontextprotocol/server-git
        Unknown bare names raise SystemExit.
    """
    spec = spec.strip()
    if not spec:
        raise SystemExit("empty --mcp spec")

    if ":" in spec:
        name, _, invocation = spec.partition(":")
        name = name.strip()
        invocation = invocation.strip()
        if not name or not invocation:
            raise SystemExit(f"malformed --mcp spec: {spec!r}")
        try:
            parts = _split_mcp_invocation(invocation)
        except ValueError as exc:
            raise SystemExit(f"malformed --mcp spec: {spec!r}: {exc}") from exc
        if not parts:
            raise SystemExit(f"malformed --mcp spec: {spec!r}")
        secret_arg = find_inline_secret_arg(parts)
        if secret_arg is not None:
            raise SystemExit(
                f"--mcp inline command for server {name!r} contains secret-looking "
                f"argv {secret_arg!r}; use --mcp-env {name}:ENVVAR and configure "
                "the server to read that environment variable instead"
            )
        if name == "filesystem" and len(parts) == 1:
            filesystem_preset = _MCP_PRESETS["filesystem"]
            return McpServerConfig(
                name=filesystem_preset.name,
                command=filesystem_preset.command,
                args=(*filesystem_preset.args[:-1], parts[0]),
            )
        return McpServerConfig(
            name=name,
            command=parts[0],
            args=tuple(parts[1:]),
        )

    # Bare name → preset.
    preset = _MCP_PRESETS.get(spec)
    if preset is None:
        raise SystemExit(
            f"unknown MCP preset {spec!r}. "
            f"Use 'name:<command>' form or pick one of: "
            f"{sorted(_MCP_PRESETS)}"
        )
    return preset


def _apply_mcp_env_overlays(
    configs: list[McpServerConfig],
    specs: list[str] | tuple[str, ...],
) -> list[McpServerConfig]:
    """Copy named parent env vars into specific MCP children."""
    if not specs:
        return configs
    by_name = {cfg.name: cfg for cfg in configs}
    if len(by_name) != len(configs):
        raise SystemExit("duplicate MCP server names are not allowed")

    for spec in specs:
        server, sep, env_name = spec.strip().partition(":")
        server = server.strip()
        env_name = env_name.strip()
        if not sep or not server or not env_name:
            raise SystemExit("malformed --mcp-env spec; expected SERVER:ENVVAR")
        cfg = by_name.get(server)
        if cfg is None:
            raise SystemExit(f"--mcp-env references unknown MCP server {server!r}")
        credential_env = tuple(dict.fromkeys((*cfg.credential_env, env_name)))
        try:
            by_name[server] = replace(cfg, credential_env=credential_env)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

    return [by_name[cfg.name] for cfg in configs]


def _split_mcp_invocation(invocation: str) -> list[str]:
    """Split an MCP command string without invoking a shell."""
    parts = shlex.split(invocation, posix=os.name != "nt")
    if os.name == "nt":
        parts = [_strip_surrounding_quotes(part) for part in parts]
    return parts


def _strip_surrounding_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


_MCP_PRESETS: dict[str, McpServerConfig] = {
    "filesystem": McpServerConfig(
        name="filesystem",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", "."),
    ),
    "github": McpServerConfig(
        name="github",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-github"),
        credential_env=_GITHUB_MCP_CREDENTIAL_ENV,
    ),
    "git": McpServerConfig(
        name="git",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-git"),
    ),
}


# ── Default system prompt ──────────────────────────────────────────────────


def _mcp_configs_from_metadata(meta: dict) -> list[McpServerConfig]:
    """Recreate MCP server configs from a session's metadata block.

    Codex review fix #3: ``ctx resume`` was creating a router from
    scratch with no MCP servers, so a resumed session lost access to
    every tool the original run had. This helper reads the session's
    recorded MCP server list (a list of
    ``{name, command, args[, env, credential_env]}`` dicts written by
    ``cmd_run`` under either the ``mcp`` or
    ``mcp_servers`` key) and reconstructs the configs.

    Tolerates missing/malformed metadata — returns ``[]`` rather than
    raising, so resume on an old session without recorded MCP info
    still works (just without MCP tools).
    """
    raw = meta.get("mcp") or meta.get("mcp_servers") or []
    if not isinstance(raw, list):
        return []
    out: list[McpServerConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        command = entry.get("command")
        if not isinstance(name, str) or not isinstance(command, str):
            continue
        args = entry.get("args") or []
        env = entry.get("env") or {}
        credential_env = entry.get("credential_env") or []
        if not isinstance(args, list):
            args = []
        if not isinstance(env, dict):
            env = {}
        if not isinstance(credential_env, list):
            credential_env = []
        try:
            out.append(
                McpServerConfig(
                    name=name,
                    command=command,
                    args=tuple(str(a) for a in args),
                    env={str(k): str(v) for k, v in env.items()} if env else {},
                    credential_env=tuple(str(v) for v in credential_env),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


_LEGACY_DEFAULT_SYSTEM_PROMPT = """\
You are a coding assistant running inside the ctx harness. You have
access to the model's knowledge PLUS a set of tools for file system
access, git operations, and the ctx knowledge graph (ctx__*). The
knowledge graph tools can recommend relevant skills, agents, and MCP
servers for the user's task — use them when you need deeper expertise
or tooling you don't have loaded.

Workflow:
  1. Read the task carefully.
  2. If the task needs specialised tooling or techniques, call
     ctx__recommend_bundle(query=<short description>) to surface
     relevant skills / agents / MCPs.
  3. Use ctx__wiki_get(slug=<slug>) to read the details of a
     recommended skill/agent you want to use.
  4. Use filesystem / git / other MCP tools as needed to make
     changes.
  5. When the task is done OR you cannot proceed without more input
     from the user, answer in text — do not call more tools.

Be concise. Preserve file paths and slugs verbatim in your responses.
"""


_DEFAULT_SYSTEM_PROMPT = """\
You are a coding assistant running inside the ctx harness. Use only the
tools attached to the current request, and use them only when they are
relevant to the user's task.

Workflow:
  1. Read the task carefully.
  2. Use attached filesystem, git, knowledge, or MCP tools as needed.
  3. Make and verify the requested changes.
  4. When done or blocked on user input, answer in text.

Be concise. Preserve file paths and slugs verbatim in your responses.
"""


def _without_ctx_session_instructions(system_prompt: str) -> str:
    prompt = system_prompt
    managed_marker = "\n\n" + _CTX_TOOL_INSTRUCTIONS_START
    if managed_marker in prompt:
        prompt = prompt.split(managed_marker, 1)[0]
    legacy_marker = "\n\n" + _CTX_SESSION_MARKER
    if legacy_marker in prompt:
        prompt = prompt.split(legacy_marker, 1)[0]
    legacy_default = _LEGACY_DEFAULT_SYSTEM_PROMPT.rstrip()
    if prompt.startswith(legacy_default):
        prompt = _DEFAULT_SYSTEM_PROMPT.rstrip() + prompt[len(legacy_default) :]
    return prompt.rstrip()


def _with_ctx_session_instructions(
    system_prompt: str,
    session_id: str,
    definitions: list[ToolDefinition],
) -> str:
    base_prompt = _without_ctx_session_instructions(system_prompt)
    if not base_prompt:
        return ""
    names = {definition.name for definition in definitions}
    instructions: list[str] = []
    if "ctx__recommend_bundle" in names:
        instructions.append(
            "Call ctx__recommend_bundle only when the task needs relevant skills, "
            "agents, or MCP servers that are not already active."
        )
    if "ctx__wiki_get" in names:
        instructions.append("Use ctx__wiki_get to inspect a recommended entity before choosing it.")
    if "ctx__load_entity" in names:
        instructions.append(
            "Record ctx__load_entity when the user/host chooses a recommended "
            "skill, agent, MCP server, or harness."
        )
    if "ctx__mark_entity_used" in names:
        instructions.append(
            "Record ctx__mark_entity_used when it materially helps; include "
            "ctx__mark_entity_used.token_usage only when exact per-entity usage "
            "is available, and do not allocate session totals across tools."
        )
    if "ctx__unload_entity" in names:
        instructions.append(
            "Call ctx__unload_entity only after user confirmation or an explicit "
            "skip/unload instruction."
        )
    if not instructions:
        return base_prompt
    lifecycle_names = {
        "ctx__load_entity",
        "ctx__mark_entity_used",
        "ctx__unload_entity",
    }
    lines = [_CTX_TOOL_INSTRUCTIONS_START, *instructions]
    if names & lifecycle_names:
        lines.extend(
            [
                f"{_CTX_SESSION_MARKER} {session_id}",
                "Use this exact session_id when calling ctx lifecycle tools.",
            ]
        )
    lines.append(_CTX_TOOL_INSTRUCTIONS_END)
    return base_prompt + "\n\n" + "\n".join(lines) + "\n"


def _resume_messages_with_system_prompt(messages: tuple[Any, ...], system_prompt: str) -> list[Any]:
    replay = list(messages)
    if replay and replay[0].role == "system":
        if system_prompt:
            replay[0] = replace(replay[0], content=system_prompt)
        else:
            replay.pop(0)
    return replay


def _record_lifecycle_safely(
    lifecycle: RuntimeLifecycleStore,
    method_name: str,
    **kwargs: Any,
) -> None:
    started = time.perf_counter()
    try:
        method = getattr(lifecycle, method_name)
        method(**kwargs)
    except Exception as exc:  # noqa: BLE001 - lifecycle must not break the runtime.
        _logger.warning("ctx runtime lifecycle record failed: %s", exc)
        _record_cli_telemetry(
            "ctx.runtime_lifecycle.write",
            session_id=str(kwargs.get("session_id") or "") or None,
            phase="failed",
            payload={"ctx.lifecycle.method": method_name},
            outcome="error",
            duration_ms=_duration_ms(started),
            error_kind=type(exc).__name__,
        )


def _agent_token_usage(usage: Usage, *, model: str | None, provider: str) -> dict[str, Any]:
    tokens_reported = usage.input_tokens > 0 or usage.output_tokens > 0
    return {
        "attribution": "exact" if tokens_reported else "unavailable",
        "input_tokens": usage.input_tokens if tokens_reported else None,
        "output_tokens": usage.output_tokens if tokens_reported else None,
        "total_tokens": usage.input_tokens + usage.output_tokens if tokens_reported else None,
        "cost_usd": usage.cost_usd,
        "attribution_reason": None if tokens_reported else "provider reported no token usage",
        "model": model,
        "provider": provider,
    }


def _load_runtime_agent(
    lifecycle: RuntimeLifecycleStore,
    *,
    session_id: str,
    role: str,
) -> None:
    common = {"session_id": session_id, "entity_type": "agent", "slug": role}
    _record_lifecycle_safely(
        lifecycle,
        "load_entity",
        **common,
        reason="explicit CLI agent flag",
        selected=True,
        selection_source="user",
        source_context={"surface": "ctx-run"},
    )
    _record_lifecycle_safely(
        lifecycle,
        "mark_entity_loaded",
        **common,
        reason="agent provider call enabled",
    )


def _mark_runtime_agent_used(
    lifecycle: RuntimeLifecycleStore,
    *,
    session_id: str,
    role: str,
    usage: Usage,
    model: str | None,
    provider: str,
) -> None:
    _record_lifecycle_safely(
        lifecycle,
        "mark_entity_used",
        session_id=session_id,
        entity_type="agent",
        slug=role,
        evidence="explicit agent provider call completed",
        token_usage=_agent_token_usage(usage, model=model, provider=provider),
    )


def _unload_runtime_agent(
    lifecycle: RuntimeLifecycleStore,
    *,
    session_id: str,
    role: str,
) -> None:
    common = {"session_id": session_id, "entity_type": "agent", "slug": role}
    _record_lifecycle_safely(
        lifecycle,
        "unload_entity",
        **common,
        reason="bounded agent call complete",
    )
    _record_lifecycle_safely(
        lifecycle,
        "mark_entity_unloaded",
        **common,
        reason="agent provider surface released",
    )


def _write_session_config_safely(
    store: SessionStore,
    config: dict[str, Any],
) -> None:
    try:
        store.write_session_config(config)
    except Exception as exc:  # noqa: BLE001 - cleanup must preserve the primary failure.
        _logger.warning("ctx session metadata write failed: %s", exc)


def _duration_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _record_cli_telemetry(
    event_name: str,
    *,
    session_id: str | None,
    phase: str,
    payload: dict[str, Any],
    outcome: str,
    duration_ms: float,
    error_kind: str | None = None,
    exc: BaseException | None = None,
) -> None:
    event_payload = dict(payload)
    event_payload["ctx.run.phase"] = phase
    event_payload["otel.status_code"] = "ERROR" if outcome == "error" else "OK"
    if error_kind:
        event_payload["error.type"] = error_kind
    try:
        if exc is not None:
            record_exception(
                event_name,
                source="ctx-cli",
                exc=exc,
                transport="cli",
                actor="user",
                session_id=session_id,
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                cwd=str(Path.cwd()),
                payload=event_payload,
            )
        else:
            record_event(
                event_name,
                source="ctx-cli",
                transport="cli",
                actor="user",
                session_id=session_id,
                outcome=outcome,
                duration_ms=duration_ms,
                error_kind=error_kind,
                cwd=str(Path.cwd()),
                payload=event_payload,
            )
    except Exception:  # noqa: BLE001 - telemetry must never break the CLI.
        pass


def _run_start_payload(
    args: argparse.Namespace,
    *,
    ctx_tools_enabled: bool,
    mcp_count: int,
    allow_count: int,
    deny_count: int,
    plan_available: bool,
) -> dict[str, Any]:
    return {
        "ctx.model": args.model,
        "ctx.provider": args.provider or _model_provider_prefix(args.model),
        "ctx.provider_prefix": _model_provider_prefix(args.model),
        "ctx.task.length": len(str(args.task or "")),
        "ctx.ctx_tools.enabled": ctx_tools_enabled,
        "ctx.mcp.count": mcp_count,
        "ctx.planner.enabled": bool(args.planner),
        "ctx.planner.result_available": plan_available,
        "ctx.evaluator.enabled": bool(args.evaluator),
        "ctx.contract.enabled": bool(args.contract),
        "ctx.budget.usd_configured": args.budget_usd is not None,
        "ctx.budget.tokens_configured": args.budget_tokens is not None,
        "ctx.tool_policy.allow_count": allow_count,
        "ctx.tool_policy.deny_count": deny_count,
    }


def _adaptive_runtime_payload(
    controller: AdaptiveRuntimeController | _AdaptiveMcpController | None,
) -> dict[str, Any]:
    if controller is None:
        return {
            "ctx.adaptive.enabled": False,
            "ctx.adaptive.skill_selected": False,
            "ctx.adaptive.selection_duration_ms": 0.0,
            "ctx.adaptive.selected_context_bytes": 0,
            "ctx.adaptive.submitted_context_bytes": 0,
            "ctx.adaptive.estimated_selected_context_tokens": 0,
            "ctx.adaptive.mcp_configured_count": 0,
            "ctx.adaptive.mcp_activated_count": 0,
            "ctx.adaptive.mcp_fetched_tool_count": 0,
            "ctx.adaptive.mcp_submitted_tool_count": 0,
            "ctx.adaptive.mcp_submitted_schema_bytes": 0,
            "ctx.adaptive.mcp_estimated_schema_tokens": 0,
            "ctx.adaptive.mcp_schema_submission_attempted": False,
        }
    summary = controller.summary()
    return {
        "ctx.adaptive.enabled": True,
        "ctx.adaptive.skill_selected": bool(summary["skill_selected"]),
        "ctx.adaptive.selection_duration_ms": float(summary["selection_duration_ms"]),
        "ctx.adaptive.selected_context_bytes": int(summary["selected_context_bytes"]),
        "ctx.adaptive.submitted_context_bytes": int(summary["submitted_context_bytes"]),
        "ctx.adaptive.estimated_selected_context_tokens": int(
            summary["estimated_selected_context_tokens"]
        ),
        "ctx.adaptive.skill_hash": summary["skill_hash"],
        "ctx.adaptive.mcp_configured_count": int(summary.get("mcp_configured_count", 0)),
        "ctx.adaptive.mcp_activated_count": int(summary.get("mcp_activated_count", 0)),
        "ctx.adaptive.mcp_fetched_tool_count": int(summary.get("mcp_fetched_tool_count", 0)),
        "ctx.adaptive.mcp_submitted_tool_count": int(summary.get("mcp_submitted_tool_count", 0)),
        "ctx.adaptive.mcp_submitted_schema_bytes": int(
            summary.get("mcp_submitted_schema_bytes", 0)
        ),
        "ctx.adaptive.mcp_estimated_schema_tokens": int(
            summary.get("mcp_estimated_schema_tokens", 0)
        ),
        "ctx.adaptive.mcp_schema_submission_attempted": bool(
            summary.get("mcp_schema_submission_attempted", False)
        ),
    }


class _AdaptiveMcpController:
    """Compose a one-turn skill lease with exact host-granted MCP servers."""

    def __init__(
        self,
        skill: AdaptiveRuntimeController,
        *,
        router: McpRouter,
        server_names: tuple[str, ...],
        configured_count: int,
        lifecycle: RuntimeLifecycleStore,
        session_id: str,
        allow_patterns: tuple[str, ...],
        deny_patterns: tuple[str, ...],
    ) -> None:
        self._skill = skill
        self._router = router
        self._server_names = server_names
        self._configured_count = configured_count
        self._lifecycle = lifecycle
        self._session_id = session_id
        self._allow_patterns = allow_patterns
        self._deny_patterns = deny_patterns
        self._prepared_epoch: int | None = None
        self._prepared_tools: tuple[ToolDefinition, ...] = ()
        self._active_servers: tuple[str, ...] = ()
        self._mcp_consumed = False
        self._mcp_activated_count = 0
        self._mcp_fetched_tool_count = 0
        self._mcp_submitted_tool_count = 0
        self._mcp_submitted_schema_bytes = 0
        self._mcp_schema_submission_attempted = False

    @property
    def selection(self) -> SelectedSkill | None:
        return self._skill.selection

    @property
    def mcp_server_names(self) -> tuple[str, ...]:
        return self._server_names

    def summary(self) -> dict[str, Any]:
        summary = self._skill.summary()
        summary["mcp_configured_count"] = self._configured_count
        summary["mcp_activated_count"] = self._mcp_activated_count
        summary["mcp_fetched_tool_count"] = self._mcp_fetched_tool_count
        summary["mcp_submitted_tool_count"] = self._mcp_submitted_tool_count
        summary["mcp_submitted_schema_bytes"] = self._mcp_submitted_schema_bytes
        summary["mcp_estimated_schema_tokens"] = (self._mcp_submitted_schema_bytes + 3) // 4
        summary["mcp_schema_submission_attempted"] = self._mcp_schema_submission_attempted
        return summary

    def prepare_turn(
        self,
        iteration: int,
        messages: tuple[Message, ...],
        base_tools: tuple[ToolDefinition, ...],
        *,
        deadline_monotonic: float | None,
        cancel_event: threading.Event | None,
    ) -> TurnPreparation:
        preparation = self._skill.prepare_turn(
            iteration,
            messages,
            base_tools,
            deadline_monotonic=deadline_monotonic,
            cancel_event=cancel_event,
        )
        self._prepared_epoch = iteration
        self._prepared_tools = base_tools
        return preparation

    def activate_turn(
        self,
        iteration: int,
        capability_epoch: int,
    ) -> TurnActivation | Usage | None:
        skill_usage = self._skill.activate_turn(iteration, capability_epoch)
        if capability_epoch != iteration or self._prepared_epoch != iteration:
            return skill_usage
        if not self._server_names:
            return TurnActivation(
                tools=self._prepared_tools,
                usage=skill_usage or Usage(),
            )
        if self._mcp_consumed:
            return skill_usage
        tools = self._router.activate(self._server_names)
        self._active_servers = self._server_names
        self._mcp_activated_count += len(self._active_servers)
        self._mcp_fetched_tool_count = len(tools)
        for server_name in self._active_servers:
            _record_lifecycle_safely(
                self._lifecycle,
                "mark_entity_loaded",
                session_id=self._session_id,
                entity_type="mcp-server",
                slug=server_name,
                reason="adaptive MCP capability lease activated",
            )
        visible_tools = tuple(tool for tool in tools if self._tool_is_visible(tool.name))
        encoded_schemas = (
            json.dumps(
                [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    }
                    for tool in visible_tools
                ],
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if visible_tools
            else ""
        )
        self._mcp_submitted_tool_count = len(visible_tools)
        self._mcp_submitted_schema_bytes = len(encoded_schemas.encode("utf-8"))
        return TurnActivation(
            tools=(*self._prepared_tools, *visible_tools),
            usage=skill_usage or Usage(),
        )

    def on_provider_request(self, iteration: int, capability_epoch: int) -> None:
        self._skill.on_provider_request(iteration, capability_epoch)
        if self._active_servers and self._mcp_submitted_tool_count:
            self._mcp_schema_submission_attempted = True

    def authorize_tool_call(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
    ) -> TurnAuthorization | None:
        return self._skill.authorize_tool_call(iteration, capability_epoch, call)

    def on_tool_result(
        self,
        iteration: int,
        capability_epoch: int,
        call: ToolCall,
        result: str,
        error: str | None,
    ) -> Usage | None:
        server_name = call.name.split(TOOL_SEPARATOR, 1)[0] if TOOL_SEPARATOR in call.name else ""
        mcp_dispatched = error is None or error.startswith(("MCP: ", "MCP-dispatch: "))
        if server_name in self._active_servers and mcp_dispatched:
            _record_lifecycle_safely(
                self._lifecycle,
                "mark_entity_used",
                session_id=self._session_id,
                entity_type="mcp-server",
                slug=server_name,
                evidence="adaptive MCP tool call attempted",
            )
        return self._skill.on_tool_result(iteration, capability_epoch, call, result, error)

    def close_turn(
        self,
        iteration: int,
        capability_epoch: int,
        outcome: str,
    ) -> Usage | None:
        deactivation_error: Exception | None = None
        active_servers = self._active_servers
        self._active_servers = ()
        if active_servers:
            try:
                self._router.deactivate(active_servers)
            except Exception as exc:  # noqa: BLE001 - skill cleanup must still run.
                deactivation_error = exc
            else:
                for server_name in active_servers:
                    _record_lifecycle_safely(
                        self._lifecycle,
                        "mark_entity_unloaded",
                        session_id=self._session_id,
                        entity_type="mcp-server",
                        slug=server_name,
                        reason=f"adaptive MCP capability lease closed after {outcome}",
                    )
            self._mcp_consumed = True
        self._prepared_epoch = None
        self._prepared_tools = ()
        usage = self._skill.close_turn(iteration, capability_epoch, outcome)
        if deactivation_error is not None:
            raise deactivation_error
        return usage

    def _tool_is_visible(self, name: str) -> bool:
        if self._deny_patterns and any(
            fnmatchcase(name, pattern) for pattern in self._deny_patterns
        ):
            return False
        return not self._allow_patterns or any(
            fnmatchcase(name, pattern) for pattern in self._allow_patterns
        )


def _adaptive_controller_for_task(
    task: str,
    *,
    cwd: Path,
    lifecycle: RuntimeLifecycleStore,
    session_id: str,
    router: McpRouter | None = None,
    mcp_server_names: tuple[str, ...] = (),
    mcp_configured_count: int = 0,
    allow_patterns: tuple[str, ...] = (),
    deny_patterns: tuple[str, ...] = (),
) -> AdaptiveRuntimeController | _AdaptiveMcpController:
    def on_activate(selection: SelectedSkill) -> None:
        _record_lifecycle_safely(
            lifecycle,
            "mark_entity_loaded",
            session_id=session_id,
            entity_type="skill",
            slug=selection.name,
            reason="adaptive capability lease activated",
        )

    def on_deactivate(selection: SelectedSkill, outcome: str, submitted: bool) -> None:
        if submitted:
            _record_lifecycle_safely(
                lifecycle,
                "mark_entity_used",
                session_id=session_id,
                entity_type="skill",
                slug=selection.name,
                evidence="adaptive context submitted to provider",
                token_usage={
                    "attribution": "estimated",
                    "input_tokens": selection.estimated_context_tokens,
                    "output_tokens": 0,
                    "attribution_reason": "bounded character estimate for selected skill context",
                },
            )
        _record_lifecycle_safely(
            lifecycle,
            "mark_entity_unloaded",
            session_id=session_id,
            entity_type="skill",
            slug=selection.name,
            reason=f"adaptive capability lease closed after {outcome}",
        )

    controller = AdaptiveRuntimeController.from_task(
        task,
        cwd=cwd,
        on_activate=on_activate,
        on_deactivate=on_deactivate,
    )
    if router is None:
        return controller
    return _AdaptiveMcpController(
        controller,
        router=router,
        server_names=mcp_server_names,
        configured_count=mcp_configured_count,
        lifecycle=lifecycle,
        session_id=session_id,
        allow_patterns=allow_patterns,
        deny_patterns=deny_patterns,
    )


def _adaptive_mcp_server_names(
    configs: list[McpServerConfig],
    allow_patterns: tuple[str, ...],
    deny_patterns: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Return only servers that can satisfy the host's allow patterns."""
    configured = tuple(config.name for config in configs)
    if any(pattern in {"*", "**"} for pattern in deny_patterns):
        return ()
    server_patterns = tuple(
        pattern.split(TOOL_SEPARATOR, 1)[0]
        for pattern in allow_patterns
        if TOOL_SEPARATOR in pattern
    )
    allow_all = any(pattern in {"*", "**"} for pattern in allow_patterns)
    allowed = (
        configured
        if not allow_patterns or allow_all
        else tuple(
            name
            for name in configured
            if any(fnmatchcase(name, pattern) for pattern in server_patterns)
        )
    )

    def fully_denied(name: str) -> bool:
        for pattern in deny_patterns:
            if TOOL_SEPARATOR not in pattern:
                continue
            server_pattern, tool_pattern = pattern.split(TOOL_SEPARATOR, 1)
            if tool_pattern in {"*", "**"} and fnmatchcase(name, server_pattern):
                return True
        return False

    return tuple(name for name in allowed if not fully_denied(name))


def _record_adaptive_selection_request(
    lifecycle: RuntimeLifecycleStore,
    controller: AdaptiveRuntimeController | _AdaptiveMcpController | None,
    *,
    session_id: str,
) -> None:
    selection = controller.selection if controller is not None else None
    if selection is not None:
        _record_lifecycle_safely(
            lifecycle,
            "load_entity",
            session_id=session_id,
            entity_type="skill",
            slug=selection.name,
            reason="adaptive task match",
            selected=True,
            selection_source="host",
            source_context={
                "score": selection.score,
                "estimated_context_tokens": selection.estimated_context_tokens,
            },
        )
    server_names = getattr(controller, "mcp_server_names", ())
    for server_name in server_names:
        _record_lifecycle_safely(
            lifecycle,
            "load_entity",
            session_id=session_id,
            entity_type="mcp-server",
            slug=server_name,
            reason="explicit adaptive MCP grant",
            selected=True,
            selection_source="user",
            source_context={"surface": "ctx-run"},
        )


def _run_setup_failure_payload(args: argparse.Namespace, *, stage: str) -> dict[str, Any]:
    model = args.model if isinstance(args.model, str) and args.model else None
    provider_prefix = _model_provider_prefix(model) if model else None
    payload: dict[str, Any] = {
        "ctx.task.length": len(str(args.task or "")),
        "ctx.ctx_tools.enabled": not args.no_ctx_tools,
        "ctx.planner.enabled": bool(args.planner),
        "ctx.evaluator.enabled": bool(args.evaluator),
        "ctx.contract.enabled": bool(args.contract),
        "ctx.run.failure_stage": stage,
    }
    if model:
        payload["ctx.model"] = model
    provider = args.provider or provider_prefix
    if provider:
        payload["ctx.provider"] = provider
    if provider_prefix:
        payload["ctx.provider_prefix"] = provider_prefix
    return payload


def _session_initial_trace_id(meta: dict[str, Any]) -> str | None:
    value = meta.get("initial_trace_id") or meta.get("trace_id")
    if not isinstance(value, str):
        return None
    if not _TRACE_ID_RE.fullmatch(value) or value == "0" * 32:
        return None
    return value


def _with_previous_trace_id(
    payload: dict[str, Any],
    previous_trace_id: str | None,
) -> dict[str, Any]:
    if previous_trace_id:
        payload["ctx.session.previous_trace_id"] = previous_trace_id
    return payload


def _resume_start_payload(
    args: argparse.Namespace,
    *,
    model: str,
    use_ctx_tools: bool,
    prior_message_count: int,
    recorded_mcp_count: int,
    restored_mcp_count: int,
    allow_count: int,
    deny_count: int,
    previous_trace_id: str | None = None,
) -> dict[str, Any]:
    payload = {
        "ctx.model": model,
        "ctx.task.length": len(str(args.task or "")),
        "ctx.ctx_tools.enabled": use_ctx_tools,
        "ctx.messages.prior_count": prior_message_count,
        "ctx.mcp.recorded_count": recorded_mcp_count,
        "ctx.mcp.restored_count": restored_mcp_count,
        "ctx.tool_policy.allow_count": allow_count,
        "ctx.tool_policy.deny_count": deny_count,
    }
    return _with_previous_trace_id(payload, previous_trace_id)


def _resume_setup_failure_payload(
    args: argparse.Namespace,
    *,
    stage: str,
    previous_trace_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ctx.task.length": len(str(args.task or "")),
        "ctx.run.failure_stage": stage,
    }
    if args.model:
        payload["ctx.model"] = args.model
    return _with_previous_trace_id(payload, previous_trace_id)


def _loop_result_payload(result: Any) -> dict[str, Any]:
    stop_reason = str(getattr(result, "stop_reason", "error"))
    payload: dict[str, Any] = {"ctx.stop_reason": stop_reason}
    if result is None:
        return payload
    payload["ctx.iterations"] = int(getattr(result, "iterations", 0) or 0)
    usage = getattr(result, "usage", None)
    if usage is not None:
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        payload["ctx.usage.input_tokens"] = input_tokens
        payload["ctx.usage.output_tokens"] = output_tokens
        payload["ctx.usage.total_tokens"] = input_tokens + output_tokens
        payload["ctx.usage.scope"] = "session"
        payload["ctx.usage.attribution"] = "unavailable"
        payload["ctx.usage.attribution_reason"] = _SESSION_USAGE_ATTRIBUTION_REASON
        payload["ctx.usage.cost_present"] = getattr(usage, "cost_usd", None) is not None
    return payload


def _usage_attribution_summary(usage: Any) -> dict[str, Any]:
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return {
        "scope": "session",
        "attribution": "unavailable",
        "attribution_reason": _SESSION_USAGE_ATTRIBUTION_REASON,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": getattr(usage, "cost_usd", None),
    }


def _loop_result_outcome(result: Any) -> tuple[str, str | None]:
    stop_reason = str(getattr(result, "stop_reason", "error"))
    if result is None or stop_reason in _ERROR_STOP_REASONS:
        return "error", stop_reason
    return "ok", None


# ── Main entry ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "resume":
            return _cmd_resume(args)
        if args.command == "sessions":
            return _cmd_sessions(args)
    except RuntimeError as exc:
        if _is_missing_harness_extra_error(exc):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise
    parser.print_help()
    return 2


def _is_missing_harness_extra_error(exc: RuntimeError) -> bool:
    message = str(exc).lower()
    return "litellm is required" in message or "claude-ctx[harness]" in message


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctx",
        description=(
            "ctx — model-agnostic harness. Drive any LLM through "
            "a coding task with file system, git, and ctx-core skill "
            "tools attached."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    # run
    r = sub.add_parser(
        "run",
        help="Start a new agent session.",
        description="Run the harness against a fresh task.",
    )
    r.add_argument(
        "--provider",
        help=(
            "Provider backend (informational; the model string's "
            "prefix determines actual routing when using LiteLLM). "
            "Default: inferred from --model."
        ),
    )
    r.add_argument(
        "--model",
        help=(
            "Model slug in LiteLLM form, e.g. "
            "'openrouter/anthropic/claude-opus-4.7', "
            "'ollama/llama3.1:70b', 'openai/gpt-5.5'. "
            "Default: ~/.claude/ctx-model-profile.json when configured."
        ),
    )
    r.add_argument(
        "--task",
        required=True,
        help="The task for the agent (user-turn content).",
    )
    r.add_argument(
        "--system-prompt",
        help=("Override the default system prompt. Pass '-' to read from stdin."),
    )
    r.add_argument(
        "--mcp",
        action="append",
        default=[],
        metavar="NAME[:COMMAND]",
        help=(
            "Attach an MCP server. Repeatable. Forms: "
            "'filesystem' (preset) or 'name:npx -y ...' (explicit; "
            "secret-looking argv rejected)."
        ),
    )
    r.add_argument(
        "--mcp-env",
        action="append",
        default=[],
        metavar="SERVER:ENVVAR",
        help=(
            "Pass one named parent environment variable to one MCP "
            "server without inheriting the full process environment. "
            "Repeatable."
        ),
    )
    r.add_argument(
        "--no-ctx-tools",
        action="store_true",
        help="Do not attach the built-in ctx__* tool surface.",
    )
    _add_ctx_tool_surface_arg(r, default="adaptive")
    _add_tool_policy_args(r)
    r.add_argument(
        "--api-key-env",
        help=(
            "Override the env var holding the provider's API key. "
            "Default: auto-detected from the model prefix."
        ),
    )
    r.add_argument(
        "--base-url",
        help="Override provider base URL (e.g. Ollama host).",
    )
    r.add_argument(
        "--temperature",
        type=float,
        default=0.7,
        help="Sampling temperature (default 0.7).",
    )
    r.add_argument(
        "--max-iterations",
        type=_positive_int,
        default=25,
        help="Hard cap on agent loop iterations (default 25).",
    )
    r.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        help="Max tokens per provider call (default: provider default).",
    )
    r.add_argument(
        "--provider-timeout",
        type=_positive_float,
        default=120.0,
        help="Wall-clock timeout in seconds for each provider call (default 120).",
    )
    r.add_argument(
        "--budget-usd",
        type=_positive_float,
        default=None,
        help="Stop when cumulative cost exceeds this many USD.",
    )
    r.add_argument(
        "--budget-tokens",
        type=_positive_int,
        default=None,
        help="Stop when input+output tokens exceed this total.",
    )
    r.add_argument(
        "--no-compact",
        action="store_true",
        help="Disable automatic context compaction.",
    )
    r.add_argument(
        "--session-id",
        help="Pin the session id. Default: auto-generated uuid.",
    )
    r.add_argument(
        "--overwrite-session",
        action="store_true",
        help=(
            "Allow --session-id to replace an existing session log. "
            "Default: reject reuse to preserve transcripts."
        ),
    )
    r.add_argument(
        "--sessions-dir",
        default=None,
        help="Override sessions directory (default ~/.ctx/sessions).",
    )
    r.add_argument(
        "--planner",
        action="store_true",
        help=(
            "Run a Planner agent first to decompose the task into a "
            "structured spec before the Generator executes. Adds one "
            "provider call. Opt-in per Plan 001 §5."
        ),
    )
    r.add_argument(
        "--planner-model",
        default=None,
        help=("Model override for the planner. Default: same as --model."),
    )
    r.add_argument(
        "--evaluator",
        action="store_true",
        help=(
            "Run an Evaluator agent after the Generator finishes. "
            "Grades the output against criteria (the planner's spec "
            "criteria when --planner is set, sensible defaults otherwise). "
            "When the verdict is 'needs_revision', feeds back into "
            "the Generator for up to --evaluator-rounds revisions."
        ),
    )
    r.add_argument(
        "--evaluator-model",
        default=None,
        help=("Model override for the evaluator. Default: same as --model."),
    )
    r.add_argument(
        "--evaluator-rounds",
        type=_evaluator_rounds,
        default=2,
        help=(
            "Max Generator->Evaluator rounds (1 = one generation "
            "then a final grade, no revision; 2 = one revision; "
            "etc.). Default 2."
        ),
    )
    r.add_argument(
        "--contract",
        action="store_true",
        help=(
            "Refine the planner's success criteria into testable "
            "contract clauses before the Generator runs. Requires "
            "--planner and --evaluator (the three agents share a "
            "contract-driven definition of 'done'). Adds one "
            "provider call."
        ),
    )
    r.add_argument(
        "--contract-model",
        default=None,
        help="Model override for the contract-refinement call.",
    )
    r.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress status lines; only print the final message.",
    )
    r.add_argument(
        "--json",
        action="store_true",
        help="Emit the LoopResult as JSON instead of text.",
    )

    # resume
    rz = sub.add_parser(
        "resume",
        help="Continue a previously-run session by id.",
    )
    rz.add_argument("session_id", help="The session id to resume.")
    rz.add_argument(
        "--task",
        required=True,
        help="The follow-up task to run against the replayed session.",
    )
    rz.add_argument(
        "--model",
        help=(
            "Model to use for the resume. Default: the same model the "
            "original session used (read from session metadata)."
        ),
    )
    rz.add_argument(
        "--provider",
        help=(
            "Provider backend for API-key auto-detection. Default: the "
            "recorded provider from the original session, then model prefix."
        ),
    )
    rz.add_argument(
        "--api-key-env",
        help=(
            "Override the env var holding the provider's API key. "
            "Default: recorded session value, then auto-detected."
        ),
    )
    rz.add_argument(
        "--base-url",
        help="Override provider base URL. Default: recorded session value.",
    )
    rz.add_argument(
        "--provider-timeout",
        type=_positive_float,
        default=None,
        help="Provider-call timeout in seconds. Default: recorded value, then 120.",
    )
    rz.add_argument(
        "--sessions-dir",
        default=None,
        help="Override sessions directory.",
    )
    rz.add_argument(
        "--restore-session-mcp",
        action="store_true",
        help=(
            "Restore MCP servers recorded in the session metadata. "
            "Off by default because session logs are local files and "
            "can contain executable command metadata."
        ),
    )
    _add_ctx_tool_surface_arg(rz, default=None)
    _add_tool_policy_args(rz)
    rz.add_argument(
        "--quiet",
        action="store_true",
    )
    rz.add_argument(
        "--json",
        action="store_true",
    )

    # sessions
    ls = sub.add_parser(
        "sessions",
        help="List saved sessions or inspect one by id.",
    )
    ls.add_argument(
        "--sessions-dir",
        default=None,
        help="Override sessions directory.",
    )
    ls.add_argument(
        "session_id",
        nargs="?",
        default=None,
        help="If given, print that session's summary metadata.",
    )
    ls.add_argument(
        "--json",
        action="store_true",
    )

    return p


# ── Command: run ───────────────────────────────────────────────────────────


def _cmd_run(args: argparse.Namespace) -> int:
    telemetry_started = time.perf_counter()
    profile_error = _apply_model_profile_defaults(args)
    if profile_error:
        print(profile_error, file=sys.stderr)
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=args.session_id,
                phase="failed",
                payload=_run_setup_failure_payload(args, stage="validation"),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind="ValueError",
            )
        return 2

    if args.evaluator and args.contract and not args.planner:
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=args.session_id,
                phase="failed",
                payload=_run_setup_failure_payload(args, stage="validation"),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind="SystemExit",
            )
        raise SystemExit(
            "error: --contract requires --planner (the contract refines the "
            "planner's success_criteria into testable clauses)."
        )

    sdir = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()

    api_key_env = _resolve_api_key_env(args.api_key_env, args.model, args.provider)
    provider = get_provider(
        default_model=args.model,
        base_url=args.base_url,
        api_key_env=api_key_env,
        timeout=args.provider_timeout,
    )
    auxiliary_provider = (
        get_provider(
            default_model=args.model,
            base_url=args.base_url,
            api_key_env=api_key_env,
            timeout=min(args.provider_timeout, _MAX_AUXILIARY_AGENT_TIMEOUT),
        )
        if args.planner or args.evaluator or args.contract
        else provider
    )

    session_id = args.session_id or new_session_id()
    try:
        system_prompt = _resolve_system_prompt(args.system_prompt)
    except Exception as exc:
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=session_id,
                phase="failed",
                payload=_run_setup_failure_payload(args, stage="validation"),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
        raise

    lifecycle = RuntimeLifecycleStore()

    # Planner pass (opt-in, SOLO path only — when --evaluator is set,
    # run_with_evaluation owns the planner call so the P/G/E agents
    # share state coherently). In the solo path, the planner runs
    # inline here and the produced spec is embedded into
    # system_prompt for the Generator.
    plan_artifact = None
    if args.planner and not args.evaluator:
        if not args.quiet:
            print("[ctx] planner: building spec...", file=sys.stderr)
        planner = Planner(
            auxiliary_provider,
            model=args.planner_model or args.model,
        )
        _load_runtime_agent(lifecycle, session_id=session_id, role="planner")
        try:
            plan_artifact = planner.plan(args.task)
            _mark_runtime_agent_used(
                lifecycle,
                session_id=session_id,
                role="planner",
                usage=plan_artifact.usage,
                model=args.planner_model or args.model,
                provider=args.provider or _model_provider_prefix(args.planner_model or args.model),
            )
        except Exception as exc:
            with telemetry_span():
                _record_cli_telemetry(
                    "ctx.cli.run",
                    session_id=session_id,
                    phase="failed",
                    payload=_run_setup_failure_payload(args, stage="planner"),
                    outcome="error",
                    duration_ms=_duration_ms(telemetry_started),
                    error_kind=type(exc).__name__,
                    exc=exc,
                )
            raise
        finally:
            _unload_runtime_agent(lifecycle, session_id=session_id, role="planner")
        system_prompt = augmented_system_prompt(system_prompt, plan_artifact)
        if not args.quiet:
            status = "ok" if plan_artifact.parsed_ok else "unstructured"
            print(
                f"[ctx] planner: spec {status} "
                f"(criteria={len(plan_artifact.success_criteria)}, "
                f"risks={len(plan_artifact.risks)})",
                file=sys.stderr,
            )

    ctx_tools_enabled = not args.no_ctx_tools
    lifecycle_active = bool(ctx_tools_enabled or args.planner or args.evaluator or args.contract)
    ctx_tool_surface = _resolve_ctx_tool_surface(args.ctx_tool_surface)
    allow_tools = _normalise_tool_patterns(args.allow_tool)
    deny_tools = _normalise_tool_patterns(args.deny_tool)

    try:
        mcp_configs = _apply_mcp_env_overlays(
            [_parse_mcp_spec(spec) for spec in args.mcp],
            args.mcp_env,
        )
    except Exception as exc:
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=session_id,
                phase="failed",
                payload=_run_setup_failure_payload(args, stage="validation"),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
        raise
    router: McpRouter | None = None

    # ctx-core tools.
    extra_tools: list[ToolDefinition] = []
    tool_executor = None
    turn_controller: AdaptiveRuntimeController | _AdaptiveMcpController | None = None
    if ctx_tools_enabled:
        toolbox, ctx_definitions = _ctx_toolbox_for_surface(
            lifecycle_dir=lifecycle.root,
            bound_session_id=session_id,
            surface=ctx_tool_surface,
            allow_patterns=allow_tools,
            deny_patterns=deny_tools,
        )
        if ctx_definitions:
            extra_tools.extend(ctx_definitions)
            tool_executor = make_tool_executor(toolbox, fallback=None)
            system_prompt = _with_ctx_session_instructions(
                system_prompt,
                session_id,
                ctx_definitions,
            )
        if ctx_tool_surface == "adaptive":
            if mcp_configs:
                router = McpRouter(mcp_configs, session_id=session_id, lazy=True)
            if mcp_configs or not ctx_definitions:
                turn_controller = _adaptive_controller_for_task(
                    args.task,
                    cwd=Path.cwd(),
                    lifecycle=lifecycle,
                    session_id=session_id,
                    router=router,
                    mcp_server_names=_adaptive_mcp_server_names(
                        mcp_configs,
                        allow_tools,
                        deny_tools,
                    ),
                    mcp_configured_count=len(mcp_configs),
                    allow_patterns=allow_tools,
                    deny_patterns=deny_tools,
                )
            if not ctx_definitions:
                system_prompt = _without_ctx_session_instructions(system_prompt)
        elif not ctx_definitions:
            system_prompt = _without_ctx_session_instructions(system_prompt)
    if router is None and mcp_configs:
        router = McpRouter(mcp_configs, session_id=session_id)

    compactor = None if args.no_compact else TokenBudgetCompactor()
    tool_policy = _compile_tool_policy(allow_tools, deny_tools)

    try:
        store = SessionStore.create(
            session_id=session_id,
            sessions_dir=sdir,
            overwrite=args.overwrite_session,
        )
    except FileExistsError:
        print(
            f"error: session {session_id!r} already exists; "
            "use --overwrite-session to replace it or ctx resume to continue it.",
            file=sys.stderr,
        )
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=session_id,
                phase="failed",
                payload=_run_setup_failure_payload(args, stage="session_create"),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind="FileExistsError",
            )
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=session_id,
                phase="failed",
                payload=_run_setup_failure_payload(args, stage="session_create"),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind=type(exc).__name__,
            )
        return 1
    with telemetry_span() as span:
        metadata = {
            "task": args.task,
            "model": args.model,
            "provider": args.provider or _model_provider_prefix(args.model),
            "provider_prefix": _model_provider_prefix(args.model),
            "api_key_env": api_key_env or "",
            "base_url": args.base_url or "",
            "system_prompt": system_prompt,
            "temperature": args.temperature,
            "max_tokens": args.max_tokens,
            "provider_timeout": args.provider_timeout,
            "max_iterations": args.max_iterations,
            "budget_usd": args.budget_usd,
            "budget_tokens": args.budget_tokens,
            "initial_trace_id": span.trace_id,
            "mcp": [
                {
                    "name": c.name,
                    "command": c.command,
                    "args": list(c.args),
                    "credential_env": list(c.credential_env),
                }
                for c in mcp_configs
            ],
            "ctx_tools_enabled": ctx_tools_enabled,
            "ctx_tool_surface": ctx_tool_surface,
            "ctx_tool_names": [definition.name for definition in extra_tools],
            "ctx_adaptive": turn_controller.summary() if turn_controller else {"enabled": False},
            "tool_policy": {"allow": list(allow_tools), "deny": list(deny_tools)},
            "planner_used": plan_artifact is not None,
            "contract_used": bool(args.evaluator and args.contract),
            "evaluator_used": args.evaluator,
            "evaluator_max_rounds": args.evaluator_rounds if args.evaluator else None,
            "plan": plan_artifact.to_dict() if plan_artifact else None,
            "plan_usage": (
                {
                    "input_tokens": plan_artifact.usage.input_tokens,
                    "output_tokens": plan_artifact.usage.output_tokens,
                    "cost_usd": plan_artifact.usage.cost_usd,
                }
                if plan_artifact
                else None
            ),
        }
        observer = JsonlObserver(store, session_metadata=metadata)
        if lifecycle_active:
            _record_lifecycle_safely(
                lifecycle,
                "record_dev_event",
                session_id=session_id,
                event_type="task",
                host="ctx-run",
                cwd=str(Path.cwd()),
                payload={
                    "task": args.task,
                    "model": args.model,
                    "provider": args.provider or _model_provider_prefix(args.model),
                },
            )
            _record_adaptive_selection_request(
                lifecycle,
                turn_controller,
                session_id=session_id,
            )
        _record_cli_telemetry(
            "ctx.cli.run",
            session_id=session_id,
            phase="started",
            payload={
                **_run_start_payload(
                    args,
                    ctx_tools_enabled=ctx_tools_enabled,
                    mcp_count=len(mcp_configs),
                    allow_count=len(allow_tools),
                    deny_count=len(deny_tools),
                    plan_available=plan_artifact is not None,
                ),
                **_adaptive_runtime_payload(turn_controller),
            },
            outcome="ok",
            duration_ms=_duration_ms(telemetry_started),
        )

        if not args.quiet:
            print(f"[ctx] session {session_id}  ({store.path})", file=sys.stderr)
            print(f"[ctx] model: {args.model}", file=sys.stderr)
            if args.budget_usd is not None:
                print(f"[ctx] budget: ${args.budget_usd:.2f}", file=sys.stderr)

        evaluator_rounds: list[dict[str, Any]] | None = None
        contract_artifact = None  # populated only on P/C/G/E path
        result = None
        deferred_stop_observer: _DeferredStopObserver | None = None
        loaded_runtime_agents: list[str] = []
        try:
            if router is not None:
                if not args.quiet:
                    action = "registering dormant" if router.lazy else "starting"
                    print(
                        f"[ctx] {action} MCP servers: {[c.name for c in mcp_configs]}",
                        file=sys.stderr,
                    )
                router.start()
            if args.evaluator:
                if not args.quiet:
                    pieces = ["evaluator"]
                    if args.planner:
                        pieces.insert(0, "planner")
                    if args.contract:
                        pieces.append("contract")
                    print(
                        f"[ctx] triad enabled: {' → '.join(pieces)} "
                        f"(max_rounds={args.evaluator_rounds})",
                        file=sys.stderr,
                    )
                agent_models = {
                    "planner": args.planner_model or args.model,
                    "contract": args.contract_model or args.model,
                    "evaluator": args.evaluator_model or args.model,
                }
                enabled_agent_roles = [
                    role
                    for role, enabled in (
                        ("planner", args.planner),
                        ("contract", args.contract),
                        ("evaluator", True),
                    )
                    if enabled
                ]
                for role in enabled_agent_roles:
                    _load_runtime_agent(lifecycle, session_id=session_id, role=role)
                    loaded_runtime_agents.append(role)

                def observe_agent_usage(role: str, usage: Usage) -> None:
                    _mark_runtime_agent_used(
                        lifecycle,
                        session_id=session_id,
                        role=role,
                        usage=usage,
                        model=agent_models.get(role),
                        provider=args.provider
                        or _model_provider_prefix(agent_models.get(role) or args.model),
                    )

                planner_agent = (
                    Planner(auxiliary_provider, model=args.planner_model or args.model)
                    if args.planner
                    else None
                )
                contract_builder = (
                    ContractBuilder(
                        auxiliary_provider,
                        model=args.contract_model or args.model,
                    )
                    if args.contract
                    else None
                )
                evaluator_agent = Evaluator(
                    auxiliary_provider,
                    model=args.evaluator_model or args.model,
                )
                deferred_stop_observer = _DeferredStopObserver(observer)
                eval_outcome = run_with_evaluation(
                    provider=provider,
                    system_prompt=system_prompt,
                    task=args.task,
                    evaluator=evaluator_agent,
                    max_rounds=args.evaluator_rounds,
                    planner=planner_agent,
                    contract_builder=contract_builder,
                    router=router,
                    extra_tools=extra_tools or None,
                    tool_executor=tool_executor,
                    tool_policy=tool_policy,
                    turn_controller=turn_controller,
                    model=args.model,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    provider_timeout=args.provider_timeout,
                    max_iterations=args.max_iterations,
                    budget_usd=args.budget_usd,
                    budget_tokens=args.budget_tokens,
                    agent_usage_observer=observe_agent_usage,
                    observer=deferred_stop_observer,
                    compactor=compactor,
                )
                result = replace(eval_outcome.final, usage=eval_outcome.total_usage)
                observer.on_stop(result)
                plan_artifact = eval_outcome.plan
                contract_artifact = eval_outcome.contract
                # session_start metadata was snapshotted BEFORE the planner
                # and contract ran (they live inside run_with_evaluation),
                # so plan/contract fields on that event are null. Emit
                # explicit events here so load_session still surfaces the
                # refined artifacts for resume + audit.
                if plan_artifact is not None:
                    store.write_event("plan", plan_artifact.to_dict())
                if contract_artifact is not None:
                    store.write_event("contract", contract_artifact.to_dict())
                evaluator_rounds = [
                    {
                        "index": r.index,
                        "stop_reason": r.loop_result.stop_reason,
                        "verdict": r.evaluation.verdict,
                        "overall_score": r.evaluation.overall_score,
                        "summary_feedback": r.evaluation.summary_feedback,
                        "revision_directive": r.evaluation.revision_directive,
                        "parsed_ok": r.evaluation.parsed_ok,
                    }
                    for r in eval_outcome.rounds
                ]
                if not args.quiet:
                    last = eval_outcome.rounds[-1] if eval_outcome.rounds else None
                    if last is not None:
                        print(
                            f"[ctx] evaluator: {len(eval_outcome.rounds)} "
                            f"round(s); final verdict = {last.evaluation.verdict}",
                            file=sys.stderr,
                        )
            else:
                result = run_loop(
                    provider=provider,
                    system_prompt=system_prompt,
                    task=args.task,
                    router=router,
                    extra_tools=extra_tools or None,
                    tool_executor=tool_executor,
                    tool_policy=tool_policy,
                    turn_controller=turn_controller,
                    model=args.model,
                    temperature=args.temperature,
                    max_tokens=args.max_tokens,
                    provider_timeout=args.provider_timeout,
                    max_iterations=args.max_iterations,
                    budget_usd=args.budget_usd,
                    budget_tokens=args.budget_tokens,
                    initial_usage=plan_artifact.usage if plan_artifact is not None else None,
                    observer=observer,
                    compactor=compactor,
                )
        except Exception as exc:
            if result is None and deferred_stop_observer is not None:
                result = deferred_stop_observer.failure_result(exc)
                try:
                    observer.on_stop(result)
                except Exception:  # noqa: BLE001 - preserve the original failure.
                    _logger.exception("failed to persist evaluator failure stop")
                _write_session_config_safely(
                    store,
                    {
                        "ctx_usage": {
                            "complete": False,
                            "scope": "completed_generator_rounds",
                        }
                    },
                )
            failure_payload = {
                **_loop_result_payload(result),
                **_adaptive_runtime_payload(turn_controller),
            }
            if deferred_stop_observer is not None:
                failure_payload["ctx.usage.complete"] = False
                failure_payload["ctx.usage.scope"] = "completed_generator_rounds"
            failure_payload["ctx.evaluator.round_count"] = (
                len(evaluator_rounds) if evaluator_rounds is not None else 0
            )
            _record_cli_telemetry(
                "ctx.cli.run",
                session_id=session_id,
                phase="failed",
                payload=failure_payload,
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
            raise
        finally:
            try:
                for role in reversed(loaded_runtime_agents):
                    _unload_runtime_agent(lifecycle, session_id=session_id, role=role)
                loaded_runtime_agents.clear()
                if turn_controller is not None:
                    _write_session_config_safely(
                        store,
                        {"ctx_adaptive": turn_controller.summary()},
                    )
                if lifecycle_active:
                    _record_lifecycle_safely(
                        lifecycle,
                        "end_session",
                        session_id=session_id,
                        status=str(getattr(result, "stop_reason", "error")),
                    )
            finally:
                try:
                    store.close()
                finally:
                    if router is not None:
                        router.stop()

        outcome, error_kind = _loop_result_outcome(result)
        finish_payload = {
            **_loop_result_payload(result),
            **_adaptive_runtime_payload(turn_controller),
        }
        finish_payload["ctx.evaluator.round_count"] = (
            len(evaluator_rounds) if evaluator_rounds is not None else 0
        )
        _record_cli_telemetry(
            "ctx.cli.run",
            session_id=session_id,
            phase="finished",
            payload=finish_payload,
            outcome=outcome,
            duration_ms=_duration_ms(telemetry_started),
            error_kind=error_kind,
        )
        return _emit_result(
            result,
            session_id,
            as_json=args.json,
            quiet=args.quiet,
            evaluator_rounds=evaluator_rounds,
        )


# ── Command: resume ────────────────────────────────────────────────────────


def _load_session_for_cli(session_id: str, sessions_dir: Path) -> Any | None:
    try:
        return load_session(session_id, sessions_dir=sessions_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _cmd_resume(args: argparse.Namespace) -> int:
    telemetry_started = time.perf_counter()
    sdir = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()
    state = _load_session_for_cli(args.session_id, sdir)
    if state is None:
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.resume",
                session_id=args.session_id,
                phase="failed",
                payload=_resume_setup_failure_payload(
                    args,
                    stage="session_load",
                ),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind="SessionLoadError",
            )
        return 1

    meta = state.metadata
    previous_trace_id = _session_initial_trace_id(meta)
    model = args.model or meta.get("model")
    if not model:
        print(
            f"error: session {args.session_id!r} has no recorded model; pass --model explicitly.",
            file=sys.stderr,
        )
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.resume",
                session_id=args.session_id,
                phase="failed",
                payload=_resume_setup_failure_payload(
                    args,
                    stage="validation",
                    previous_trace_id=previous_trace_id,
                ),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind="ValueError",
            )
        return 1

    use_ctx_tools = bool(meta.get("ctx_tools_enabled", True))
    recorded_system_prompt = meta.get("system_prompt")
    system_prompt = (
        recorded_system_prompt
        if isinstance(recorded_system_prompt, str)
        else _DEFAULT_SYSTEM_PROMPT
    )
    provider_name = args.provider or meta.get("provider") or meta.get("provider_prefix")
    provider_key = provider_name if isinstance(provider_name, str) else None
    if args.api_key_env is not None:
        api_key_env = _resolve_api_key_env(args.api_key_env, model, provider_key)
    elif isinstance(meta.get("api_key_env"), str):
        api_key_env = str(meta.get("api_key_env") or "") or None
    else:
        api_key_env = _resolve_api_key_env(None, model, provider_key)
    base_url = args.base_url
    if base_url is None and isinstance(meta.get("base_url"), str):
        base_url = str(meta.get("base_url") or "") or None
    provider_timeout = args.provider_timeout
    if provider_timeout is None:
        provider_timeout = float(meta.get("provider_timeout") or 120.0)
    provider = get_provider(
        default_model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        timeout=provider_timeout,
    )

    try:
        store = SessionStore.attach(args.session_id, sessions_dir=sdir)
    except (FileNotFoundError, OSError, ValueError) as exc:
        with telemetry_span():
            _record_cli_telemetry(
                "ctx.cli.resume",
                session_id=args.session_id,
                phase="failed",
                payload=_resume_setup_failure_payload(
                    args,
                    stage="session_attach",
                    previous_trace_id=previous_trace_id,
                ),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
        raise
    observer = JsonlObserver(
        store,
        session_metadata={},
        emit_session_start=False,
        persisted_message_count=len(state.messages),
    )
    compactor = TokenBudgetCompactor()

    # Session logs are mutable local JSONL files. Recreate ctx-core
    # tools by default, but never execute MCP command metadata from a
    # transcript unless the user explicitly opts in for this resume.
    recorded_mcp_configs = _mcp_configs_from_metadata(meta)
    mcp_configs = recorded_mcp_configs if args.restore_session_mcp else []
    router: McpRouter | None = None

    lifecycle = RuntimeLifecycleStore()
    extra_tools: list[ToolDefinition] = []
    tool_executor = None
    turn_controller: AdaptiveRuntimeController | _AdaptiveMcpController | None = None
    allow_tools, deny_tools = _resume_tool_policy_patterns(args, meta)
    ctx_tool_surface = _resolve_ctx_tool_surface(
        args.ctx_tool_surface,
        meta["ctx_tool_surface"] if "ctx_tool_surface" in meta else _MISSING,
    )
    if use_ctx_tools:
        ctx_toolbox, ctx_definitions = _ctx_toolbox_for_surface(
            lifecycle_dir=lifecycle.root,
            bound_session_id=args.session_id,
            surface=ctx_tool_surface,
            allow_patterns=allow_tools,
            deny_patterns=deny_tools,
        )
        if ctx_definitions:
            extra_tools.extend(ctx_definitions)
            tool_executor = make_tool_executor(ctx_toolbox)
            system_prompt = _with_ctx_session_instructions(
                str(system_prompt),
                args.session_id,
                ctx_definitions,
            )
        if ctx_tool_surface == "adaptive":
            if mcp_configs:
                router = McpRouter(mcp_configs, session_id=args.session_id, lazy=True)
            if mcp_configs or not ctx_definitions:
                turn_controller = _adaptive_controller_for_task(
                    args.task,
                    cwd=Path.cwd(),
                    lifecycle=lifecycle,
                    session_id=args.session_id,
                    router=router,
                    mcp_server_names=_adaptive_mcp_server_names(
                        mcp_configs,
                        allow_tools,
                        deny_tools,
                    ),
                    mcp_configured_count=len(mcp_configs),
                    allow_patterns=allow_tools,
                    deny_patterns=deny_tools,
                )
            if not ctx_definitions:
                system_prompt = _without_ctx_session_instructions(str(system_prompt))
        elif not ctx_definitions:
            system_prompt = _without_ctx_session_instructions(str(system_prompt))
    else:
        system_prompt = _without_ctx_session_instructions(str(system_prompt))
    if router is None and mcp_configs:
        router = McpRouter(mcp_configs, session_id=args.session_id)
    store.write_session_config(
        {
            "system_prompt": system_prompt,
            "ctx_tools_enabled": use_ctx_tools,
            "ctx_tool_surface": ctx_tool_surface,
            "ctx_tool_names": [definition.name for definition in extra_tools],
            "ctx_adaptive": turn_controller.summary() if turn_controller else {"enabled": False},
            "tool_policy": {"allow": list(allow_tools), "deny": list(deny_tools)},
        }
    )
    resume_messages = _resume_messages_with_system_prompt(state.messages, system_prompt)
    tool_policy = _compile_tool_policy(allow_tools, deny_tools)

    with telemetry_span():
        if not args.quiet:
            bits = []
            if mcp_configs:
                bits.append(f"{len(mcp_configs)} MCP server(s)")
            elif recorded_mcp_configs:
                bits.append(f"{len(recorded_mcp_configs)} recorded MCP server(s) skipped")
            if use_ctx_tools:
                bits.append(f"ctx-core tools ({ctx_tool_surface}, {len(extra_tools)} schemas)")
            if allow_tools or deny_tools:
                bits.append(f"tool policy allow={len(allow_tools)} deny={len(deny_tools)}")
            suffix = f" + {', '.join(bits)}" if bits else ""
            print(
                f"[ctx] resuming {args.session_id} ({len(state.messages)} prior messages{suffix})",
                file=sys.stderr,
            )
            if mcp_configs:
                for cfg in mcp_configs:
                    argv = " ".join([cfg.command, *cfg.args])
                    print(
                        f"[ctx] restoring MCP server {cfg.name}: {argv}",
                        file=sys.stderr,
                    )

        if use_ctx_tools:
            _record_lifecycle_safely(
                lifecycle,
                "record_dev_event",
                session_id=args.session_id,
                event_type="resume_task",
                host="ctx-resume",
                cwd=str(Path.cwd()),
                payload={"task": args.task, "model": model},
            )
            _record_adaptive_selection_request(
                lifecycle,
                turn_controller,
                session_id=args.session_id,
            )
        _record_cli_telemetry(
            "ctx.cli.resume",
            session_id=args.session_id,
            phase="started",
            payload={
                **_resume_start_payload(
                    args,
                    model=str(model),
                    use_ctx_tools=use_ctx_tools,
                    prior_message_count=len(state.messages),
                    recorded_mcp_count=len(recorded_mcp_configs),
                    restored_mcp_count=len(mcp_configs),
                    allow_count=len(allow_tools),
                    deny_count=len(deny_tools),
                    previous_trace_id=previous_trace_id,
                ),
                **_adaptive_runtime_payload(turn_controller),
            },
            outcome="ok",
            duration_ms=_duration_ms(telemetry_started),
        )

        result = None
        try:
            if router is not None:
                router.start()
            result = run_loop(
                provider=provider,
                system_prompt=system_prompt,
                task=args.task,
                messages=resume_messages,
                model=model,
                observer=observer,
                compactor=compactor,
                router=router,
                extra_tools=extra_tools or None,
                tool_executor=tool_executor,
                tool_policy=tool_policy,
                turn_controller=turn_controller,
                # Resume must keep the replayed transcript first; the
                # follow-up task is appended at the end, not shoved before
                # the prior conversation.
                append_task_after_messages=True,
                # Inherit the original run's safety limits when present
                # so the resume doesn't blow past the original ceiling.
                max_iterations=int(meta.get("max_iterations") or 25),
                temperature=float(meta.get("temperature") or 0.7),
                max_tokens=meta.get("max_tokens"),
                provider_timeout=provider_timeout,
                budget_usd=meta.get("budget_usd"),
                budget_tokens=meta.get("budget_tokens"),
                initial_usage=state.usage,
            )
        except Exception as exc:
            _record_cli_telemetry(
                "ctx.cli.resume",
                session_id=args.session_id,
                phase="failed",
                payload=_with_previous_trace_id(
                    {
                        **_loop_result_payload(result),
                        **_adaptive_runtime_payload(turn_controller),
                    },
                    previous_trace_id,
                ),
                outcome="error",
                duration_ms=_duration_ms(telemetry_started),
                error_kind=type(exc).__name__,
                exc=exc,
            )
            raise
        finally:
            try:
                if turn_controller is not None:
                    _write_session_config_safely(
                        store,
                        {"ctx_adaptive": turn_controller.summary()},
                    )
                if use_ctx_tools:
                    _record_lifecycle_safely(
                        lifecycle,
                        "end_session",
                        session_id=args.session_id,
                        status=str(getattr(result, "stop_reason", "error")),
                    )
            finally:
                try:
                    store.close()
                finally:
                    if router is not None:
                        router.stop()

        outcome, error_kind = _loop_result_outcome(result)
        _record_cli_telemetry(
            "ctx.cli.resume",
            session_id=args.session_id,
            phase="finished",
            payload=_with_previous_trace_id(
                {
                    **_loop_result_payload(result),
                    **_adaptive_runtime_payload(turn_controller),
                },
                previous_trace_id,
            ),
            outcome=outcome,
            duration_ms=_duration_ms(telemetry_started),
            error_kind=error_kind,
        )
        return _emit_result(result, args.session_id, as_json=args.json, quiet=args.quiet)


# ── Command: sessions ─────────────────────────────────────────────────────


def _cmd_sessions(args: argparse.Namespace) -> int:
    sdir = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()
    if args.session_id is None:
        ids = list_sessions(sdir)
        if args.json:
            print(json.dumps(ids))
        else:
            if not ids:
                print("(no sessions)")
            else:
                for sid in ids:
                    print(sid)
        return 0

    # Detail view: load + summarise.
    state = _load_session_for_cli(args.session_id, sdir)
    if state is None:
        return 1
    summary = {
        "session_id": state.session_id,
        "path": str(state.path),
        "stopped": state.stopped,
        "stop_reason": state.stop_reason,
        "event_count": state.event_count,
        "messages": len(state.messages),
        "metadata": state.metadata,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"session: {state.session_id}")
        print(f"  path: {state.path}")
        print(f"  events: {state.event_count}  messages: {len(state.messages)}")
        print(f"  stopped: {state.stopped}  reason: {state.stop_reason}")
        task = state.metadata.get("task", "<no recorded task>")
        print(f"  task: {task!r}")
        model = state.metadata.get("model", "<unknown>")
        print(f"  model: {model}")
    return 0


# ── Result emission ────────────────────────────────────────────────────────


_ERROR_STOP_REASONS = frozenset(
    {
        "content_filter",
        "controller_error",
        "empty_response",
        "length",
        "observer_error",
        "provider_error",
        "provider_other",
        "provider_timeout",
        "tool_denied",
        "tool_error",
    }
)


def _emit_result(
    result: Any,
    session_id: str,
    *,
    as_json: bool,
    quiet: bool,
    evaluator_rounds: list[dict[str, Any]] | None = None,
) -> int:
    if as_json:
        payload = {
            "session_id": session_id,
            "stop_reason": result.stop_reason,
            "final_message": result.final_message,
            "iterations": result.iterations,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "cost_usd": result.usage.cost_usd,
            },
            "usage_attribution": _usage_attribution_summary(result.usage),
            "detail": result.detail,
        }
        if evaluator_rounds is not None:
            payload["evaluator_rounds"] = evaluator_rounds
        print(json.dumps(payload, indent=2))
    else:
        if not quiet:
            print(
                f"\n[ctx] stop={result.stop_reason}  iterations={result.iterations}  "
                f"tokens={result.usage.input_tokens + result.usage.output_tokens}  "
                "usage_scope=session  per_tool_usage=unavailable",
                file=sys.stderr,
            )
            if result.usage.cost_usd is not None:
                print(f"[ctx] cost: ${result.usage.cost_usd:.4f}", file=sys.stderr)
            if result.detail:
                print(f"[ctx] detail: {result.detail}", file=sys.stderr)
        print(result.final_message)

    # Non-zero only on true errors / policy blocks. Defined stops
    # (max_iterations / budget / cancellation) still exit 0 because
    # the session reached a caller-configured stopping point.
    if result.stop_reason in _ERROR_STOP_REASONS:
        return 2
    return 0


def _resolve_system_prompt(raw: str | None) -> str:
    if raw is None:
        return _DEFAULT_SYSTEM_PROMPT
    if raw == "-":
        return sys.stdin.read()
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
