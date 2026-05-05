"""ctx.cli.run — `ctx run` / `ctx resume` / `ctx sessions` CLI.

First user-facing entry to the model-agnostic harness. Ships as v1
per Plan 001 §10 success criteria:

    ctx run --provider openrouter --model minimax/minimax-m1 \\
            --mcp ctx,filesystem \\
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
import shlex
import sys
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ctx.adapters.generic.compaction import TokenBudgetCompactor
from ctx.adapters.generic.ctx_core_tools import CtxCoreToolbox, make_tool_executor
from ctx.adapters.generic.runtime_lifecycle import RuntimeLifecycleStore
from ctx.adapters.generic.loop import ToolPolicy, run_loop
from ctx.adapters.generic.contract import ContractBuilder
from ctx.adapters.generic.evaluator import Evaluator, run_with_evaluation
from ctx.adapters.generic.planner import Planner, augmented_system_prompt
from ctx.adapters.generic.providers import ToolCall, ToolDefinition, get_provider
from ctx.adapters.generic.state import (
    JsonlObserver,
    SessionStore,
    default_sessions_dir,
    list_sessions,
    load_session,
    new_session_id,
)
from ctx.adapters.generic.tools import McpRouter, McpServerConfig


_logger = logging.getLogger(__name__)
_CTX_SESSION_MARKER = "ctx runtime session id:"


# ── Provider key-env defaults ───────────────────────────────────────────────


# Tier-1 provider → env var map. The CLI reads --api-key-env or falls
# back to this table. Users can override with --api-key-env explicitly.
_PROVIDER_KEY_ENV: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
    "gemini":     "GEMINI_API_KEY",
    "mistral":    "MISTRAL_API_KEY",
    "deepseek":   "DEEPSEEK_API_KEY",
    "together":   "TOGETHER_API_KEY",
    "groq":       "GROQ_API_KEY",
    # Ollama: no key needed (local)
    "ollama":     "",
}


def _model_provider_prefix(model: str) -> str:
    """Given a model string like 'openrouter/anthropic/claude-opus-4.7',
    return the leading provider segment ('openrouter')."""
    return model.split("/", 1)[0] if "/" in model else model


def _resolve_api_key_env(
    explicit: str | None, model: str, provider: str | None,
) -> str | None:
    if explicit is not None:
        return explicit if explicit else None   # empty → None (Ollama)
    prefix = provider or _model_provider_prefix(model)
    key = _PROVIDER_KEY_ENV.get(prefix)
    return key if key else None


# ── MCP spec parsing ───────────────────────────────────────────────────────


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
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
            "If omitted, all attached tools are allowed unless denied."
        ),
    )
    parser.add_argument(
        "--deny-tool",
        action="append",
        default=[],
        metavar="PATTERN",
        help=(
            "Deny tool names matching this glob pattern before execution. "
            "Repeatable; deny rules override allow rules."
        ),
    )


def _parse_mcp_spec(spec: str) -> McpServerConfig:
    """Parse a --mcp argument.

    Two forms:
      name:<shell-invocation>
        Example: filesystem:npx -y @modelcontextprotocol/server-filesystem /data
        The part before the colon is the name; the part after is the
        command + args (split on whitespace, no shell).

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
    recorded MCP server list (a list of ``{name, command, args[, env]}``
    dicts written by ``cmd_run`` under either the ``mcp`` or
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
        if not isinstance(args, list):
            args = []
        if not isinstance(env, dict):
            env = {}
        try:
            out.append(McpServerConfig(
                name=name,
                command=command,
                args=tuple(str(a) for a in args),
                env={str(k): str(v) for k, v in env.items()} if env else {},
            ))
        except (TypeError, ValueError):
            continue
    return out


_DEFAULT_SYSTEM_PROMPT = """\
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


def _with_ctx_session_instructions(system_prompt: str, session_id: str) -> str:
    if _CTX_SESSION_MARKER in system_prompt:
        return system_prompt
    return (
        system_prompt.rstrip()
        + "\n\n"
        + "ctx runtime session id: "
        + session_id
        + "\n"
        + "Use this exact session_id when calling ctx lifecycle tools. "
        + "Record ctx__load_entity when the user/host chooses a recommended "
        + "skill, agent, MCP server, or harness; record ctx__mark_entity_used "
        + "when it materially helps; call ctx__unload_entity only after user "
        + "confirmation or an explicit skip/unload instruction.\n"
    )


def _record_lifecycle_safely(
    lifecycle: RuntimeLifecycleStore,
    method_name: str,
    **kwargs: Any,
) -> None:
    method = getattr(lifecycle, method_name)
    try:
        method(**kwargs)
    except (OSError, ValueError) as exc:
        _logger.warning("ctx runtime lifecycle record failed: %s", exc)


# ── Main entry ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "resume":
        return _cmd_resume(args)
    if args.command == "sessions":
        return _cmd_sessions(args)
    parser.print_help()
    return 2


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
        required=True,
        help=(
            "Model slug in LiteLLM form, e.g. "
            "'openrouter/anthropic/claude-opus-4.7', "
            "'ollama/llama3.1:70b', 'openai/gpt-5.5'."
        ),
    )
    r.add_argument(
        "--task", required=True,
        help="The task for the agent (user-turn content).",
    )
    r.add_argument(
        "--system-prompt",
        help=(
            "Override the default system prompt. Pass '-' to read "
            "from stdin."
        ),
    )
    r.add_argument(
        "--mcp",
        action="append",
        default=[],
        metavar="NAME[:COMMAND]",
        help=(
            "Attach an MCP server. Repeatable. Forms: "
            "'filesystem' (preset) or 'name:npx -y ...' (explicit)."
        ),
    )
    r.add_argument(
        "--no-ctx-tools",
        action="store_true",
        help="Do not attach the built-in ctx__* tool surface.",
    )
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
        "--temperature", type=float, default=0.7,
        help="Sampling temperature (default 0.7).",
    )
    r.add_argument(
        "--max-iterations", type=_positive_int, default=25,
        help="Hard cap on agent loop iterations (default 25).",
    )
    r.add_argument(
        "--max-tokens", type=_positive_int, default=None,
        help="Max tokens per provider call (default: provider default).",
    )
    r.add_argument(
        "--budget-usd", type=_positive_float, default=None,
        help="Stop when cumulative cost exceeds this many USD.",
    )
    r.add_argument(
        "--budget-tokens", type=_positive_int, default=None,
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
        help=(
            "Model override for the planner. Default: same as --model."
        ),
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
        help=(
            "Model override for the evaluator. Default: same as --model."
        ),
    )
    r.add_argument(
        "--evaluator-rounds",
        type=_positive_int,
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
        "--quiet", action="store_true",
        help="Suppress status lines; only print the final message.",
    )
    r.add_argument(
        "--json", action="store_true",
        help="Emit the LoopResult as JSON instead of text.",
    )

    # resume
    rz = sub.add_parser(
        "resume",
        help="Continue a previously-run session by id.",
    )
    rz.add_argument("session_id", help="The session id to resume.")
    rz.add_argument(
        "--task", required=True,
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
        "--sessions-dir", default=None,
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
    _add_tool_policy_args(rz)
    rz.add_argument(
        "--quiet", action="store_true",
    )
    rz.add_argument(
        "--json", action="store_true",
    )

    # sessions
    ls = sub.add_parser(
        "sessions",
        help="List saved sessions or inspect one by id.",
    )
    ls.add_argument(
        "--sessions-dir", default=None,
        help="Override sessions directory.",
    )
    ls.add_argument(
        "session_id", nargs="?", default=None,
        help="If given, print that session's summary metadata.",
    )
    ls.add_argument(
        "--json", action="store_true",
    )

    return p


# ── Command: run ───────────────────────────────────────────────────────────


def _cmd_run(args: argparse.Namespace) -> int:
    sdir = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()

    api_key_env = _resolve_api_key_env(args.api_key_env, args.model, args.provider)
    provider = get_provider(
        default_model=args.model,
        base_url=args.base_url,
        api_key_env=api_key_env,
    )

    session_id = args.session_id or new_session_id()
    system_prompt = _resolve_system_prompt(args.system_prompt)

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
            provider,
            model=args.planner_model or args.model,
        )
        plan_artifact = planner.plan(args.task)
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
    if ctx_tools_enabled:
        system_prompt = _with_ctx_session_instructions(system_prompt, session_id)

    mcp_configs = [_parse_mcp_spec(spec) for spec in args.mcp]
    router = McpRouter(mcp_configs) if mcp_configs else None

    # ctx-core tools.
    lifecycle = RuntimeLifecycleStore()
    extra_tools = []
    tool_executor = None
    if ctx_tools_enabled:
        toolbox = CtxCoreToolbox(lifecycle_dir=lifecycle.root)
        extra_tools.extend(toolbox.tool_definitions())
        tool_executor = make_tool_executor(toolbox, fallback=None)

    compactor = None if args.no_compact else TokenBudgetCompactor()
    allow_tools = _normalise_tool_patterns(args.allow_tool)
    deny_tools = _normalise_tool_patterns(args.deny_tool)
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
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
        "max_iterations": args.max_iterations,
        "budget_usd": args.budget_usd,
        "budget_tokens": args.budget_tokens,
        "mcp": [{"name": c.name, "command": c.command, "args": list(c.args)}
                for c in mcp_configs],
        "ctx_tools_enabled": ctx_tools_enabled,
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
    if ctx_tools_enabled:
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

    if not args.quiet:
        print(f"[ctx] session {session_id}  ({store.path})", file=sys.stderr)
        print(f"[ctx] model: {args.model}", file=sys.stderr)
        if args.budget_usd is not None:
            print(f"[ctx] budget: ${args.budget_usd:.2f}", file=sys.stderr)

    evaluator_rounds: list[dict[str, Any]] | None = None
    contract_artifact = None  # populated only on P/C/G/E path
    result = None
    try:
        if router is not None:
            if not args.quiet:
                print(
                    f"[ctx] starting MCP servers: {[c.name for c in mcp_configs]}",
                    file=sys.stderr,
                )
            router.start()
        if args.evaluator:
            if args.contract and not args.planner:
                # Contracts refine planner output; without a plan
                # they'd have no prior spec to refine.
                raise SystemExit(
                    "error: --contract requires --planner (the contract "
                    "refines the planner's success_criteria into "
                    "testable clauses)."
                )
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
            planner_agent = (
                Planner(provider, model=args.planner_model or args.model)
                if args.planner
                else None
            )
            contract_builder = (
                ContractBuilder(
                    provider, model=args.contract_model or args.model,
                )
                if args.contract
                else None
            )
            evaluator_agent = Evaluator(
                provider, model=args.evaluator_model or args.model,
            )
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
                model=args.model,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                max_iterations=args.max_iterations,
                budget_usd=args.budget_usd,
                budget_tokens=args.budget_tokens,
                observer=observer,
                compactor=compactor,
            )
            result = eval_outcome.final
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
                model=args.model,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                max_iterations=args.max_iterations,
                budget_usd=args.budget_usd,
                budget_tokens=args.budget_tokens,
                observer=observer,
                compactor=compactor,
            )
    finally:
        if ctx_tools_enabled:
            _record_lifecycle_safely(
                lifecycle,
                "end_session",
                session_id=session_id,
                status=str(getattr(result, "stop_reason", "error")),
            )
        store.close()
        if router is not None:
            router.stop()

    return _emit_result(
        result, session_id,
        as_json=args.json, quiet=args.quiet,
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
    sdir = Path(args.sessions_dir) if args.sessions_dir else default_sessions_dir()
    state = _load_session_for_cli(args.session_id, sdir)
    if state is None:
        return 1

    meta = state.metadata
    model = args.model or meta.get("model")
    if not model:
        print(
            f"error: session {args.session_id!r} has no recorded model; "
            "pass --model explicitly.",
            file=sys.stderr,
        )
        return 1

    use_ctx_tools = bool(meta.get("ctx_tools_enabled", True))
    system_prompt = meta.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT
    if use_ctx_tools:
        system_prompt = _with_ctx_session_instructions(
            str(system_prompt),
            args.session_id,
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
    provider = get_provider(
        default_model=model,
        base_url=base_url,
        api_key_env=api_key_env,
    )

    store = SessionStore.attach(args.session_id, sessions_dir=sdir)
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
    router = McpRouter(mcp_configs) if mcp_configs else None

    lifecycle = RuntimeLifecycleStore()
    extra_tools: list[ToolDefinition] = []
    tool_executor = None
    if use_ctx_tools:
        ctx_toolbox = CtxCoreToolbox(lifecycle_dir=lifecycle.root)
        extra_tools.extend(ctx_toolbox.tool_definitions())
        tool_executor = make_tool_executor(ctx_toolbox)
    allow_tools, deny_tools = _resume_tool_policy_patterns(args, meta)
    tool_policy = _compile_tool_policy(allow_tools, deny_tools)

    if not args.quiet:
        bits = []
        if mcp_configs:
            bits.append(f"{len(mcp_configs)} MCP server(s)")
        elif recorded_mcp_configs:
            bits.append(
                f"{len(recorded_mcp_configs)} recorded MCP server(s) skipped"
            )
        if use_ctx_tools:
            bits.append("ctx-core tools")
        if allow_tools or deny_tools:
            bits.append(
                f"tool policy allow={len(allow_tools)} deny={len(deny_tools)}"
            )
        suffix = f" + {', '.join(bits)}" if bits else ""
        print(
            f"[ctx] resuming {args.session_id} "
            f"({len(state.messages)} prior messages{suffix})",
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

    result = None
    try:
        if router is not None:
            router.start()
        result = run_loop(
            provider=provider,
            system_prompt=system_prompt,
            task=args.task,
            messages=list(state.messages),
            model=model,
            observer=observer,
            compactor=compactor,
            router=router,
            extra_tools=extra_tools or None,
            tool_executor=tool_executor,
            tool_policy=tool_policy,
            # Resume must keep the replayed transcript first; the
            # follow-up task is appended at the end, not shoved before
            # the prior conversation.
            append_task_after_messages=True,
            # Inherit the original run's safety limits when present
            # so the resume doesn't blow past the original ceiling.
            max_iterations=int(meta.get("max_iterations") or 25),
            temperature=float(meta.get("temperature") or 0.7),
            max_tokens=meta.get("max_tokens"),
            budget_usd=meta.get("budget_usd"),
            budget_tokens=meta.get("budget_tokens"),
        )
    finally:
        if use_ctx_tools:
            _record_lifecycle_safely(
                lifecycle,
                "end_session",
                session_id=args.session_id,
                status=str(getattr(result, "stop_reason", "error")),
            )
        store.close()
        if router is not None:
            router.stop()

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


def _emit_result(
    result: Any, session_id: str, *, as_json: bool, quiet: bool,
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
            "detail": result.detail,
        }
        if evaluator_rounds is not None:
            payload["evaluator_rounds"] = evaluator_rounds
        print(json.dumps(payload, indent=2))
    else:
        if not quiet:
            print(
                f"\n[ctx] stop={result.stop_reason}  iterations={result.iterations}  "
                f"tokens={result.usage.input_tokens + result.usage.output_tokens}",
                file=sys.stderr,
            )
            if result.usage.cost_usd is not None:
                print(f"[ctx] cost: ${result.usage.cost_usd:.4f}", file=sys.stderr)
            if result.detail:
                print(f"[ctx] detail: {result.detail}", file=sys.stderr)
        print(result.final_message)

    # Non-zero only on true errors / policy blocks. Everything else
    # (including max_iterations / budget) exits 0 since the session
    # ran to a defined stopping point. Cancellation also exits 0.
    if result.stop_reason in {"tool_error", "tool_denied"}:
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
