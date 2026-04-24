# Plan 001 — Model-Agnostic Harness & Repo Reorganization

**Status:** APPROVED (2026-04-24) — in execution
**Author:** ctx
**Date:** 2026-04-24
**Reference:** [Anthropic — Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps)

## Decisions (locked 2026-04-24)

| # | Decision |
|---|---|
| 1 | **Option B — full harness**. ctx *becomes* the harness around any LLM. |
| 2 | **All reorg phases R0–R6**. |
| 3 | **Providers: Ollama (local) + OpenRouter (remote aggregator) as tier-1.** Any OpenRouter-listed model (GPT-5.5, MiniMax, Claude, Gemini, Llama, etc.) works without extra adapters. Direct provider SDKs added only if OpenRouter has a gap. |
| 4 | **Both `~/.ctx/` + `~/.claude/` shipped with configs out of the box.** First `ctx`/`ctx run` invocation works without the user filling in anything. |
| 5 | **B-full reached incrementally ("Anthropic style")** — solo agent → Planner → Evaluator → sprint contracts. Each step ships as its own phase and is only added after measuring the previous step's failure modes. Target end-state is the full three-agent harness; path there is evidence-driven. |
| 6 | **State = JSONL append-only** (Claude Agent SDK pattern). State behind a `StateStore` Protocol so SQLAlchemy/Redis implementations can drop in later. **No LangGraph** — adopting it means adopting its whole graph-based programming model. |
| 7 | **Publish as pip package** after R6 completes (package name TBD: `ctx-harness` candidate). |

---

## 0. TL;DR

`ctx` is currently built as a **plugin into Claude Code** — it assumes `~/.claude/`, shells out to `claude mcp add/remove`, emits CC's `PostToolUse`/`Stop` hook JSON. Goal: allow `ctx`'s alive-skill / knowledge-graph / recommendation system to run around **any** model (MiniMax, GPT-5.5, local Ollama, etc.).

**Three interpretations of "use ctx as a harness" — decision needed before we build:**

1. **(A) Plugin-for-any-harness** — ctx stays a skill/MCP recommendation *library*; ship adapters for Claude Code (today), Aider, Goose, Cline, and any custom host. No agent loop, no while-loop, no provider management. Smallest delta.
2. **(B) Full harness** — ctx *becomes* the harness. We write the while-loop, provider adapter (via LiteLLM), tool dispatcher, context compactor, checkpointer. Claude Code integration becomes one of N hosts. Largest delta; overlaps with OpenAI Agents SDK / LangGraph / SWE-agent.
3. **(C) Hybrid, minimal harness + plugin retained** — Build a *thin* harness (the while-loop + tool dispatch + LiteLLM provider shim) that uses ctx-core's recommendation system. Package the Claude-Code integration as one adapter, generic harness as another. ~80% of option A's simplicity + an actually usable "run against any model" path.

Recommendation: **(C)**. Rationale at §3.

Second question: **repo reorganization**. 60+ flat modules under `src/`, tests embedded, no namespace. Proposal: migrate to `src/ctx/{core,adapters,cli,utils}/` over 6 phases. Details at §6.

---

## 1. What Anthropic means by "harness"

Per the article, a harness is "an orchestration layer that structures how agents execute complex tasks over extended periods." It is scaffolding that compensates for what a model can't yet do alone — and should be systematically stripped away as model capabilities improve ("every component in a harness encodes an assumption about what the model can't do on its own").

Core responsibilities Anthropic enumerates:

| Concern | What it does | In ctx today? |
|---|---|---|
| **Context management** | Compaction, resets, avoiding "context anxiety" near token limits | ❌ (Claude Code handles it) |
| **Task decomposition** | Planner → Generator → Evaluator split | ❌ |
| **State persistence** | Artifacts survive context resets | ⚠️ (skill-manifest.json is persistent; no mid-session artifacts) |
| **QA feedback loops** | Separate evaluator from generator (avoids self-praise) | ❌ |
| **Tool/env integration** | Playwright MCP, git, runtime inspection | ⚠️ (MCP install works, but we don't *orchestrate* tool use) |
| **Interactive testing** | Live browser, code execution during the run | ❌ |
| **Observability** | Read each agent's logs, tune prompts | ⚠️ (telemetry exists, but for installs not for agent runs) |
| **Cost control** | Track $/run, remove scaffolding as models improve | ❌ |
| **Long-running coordination** | Sprint contracts, checkpoints, resumability | ❌ |

The article's **recommended pattern** is the three-agent architecture: **Planner** (expands vague prompts into detailed specs) → **Generator** (implements iteratively against spec) → **Evaluator** (interactive testing, explicit grading criteria, few-shot calibrated). They communicate via **file-based artifacts**, not inline conversation.

Anti-patterns called out: solo agent on complex tasks, self-evaluation, vague grading criteria, overspecifying implementation upfront, ignoring model-specific limitations.

---

## 2. Existing harnesses — design space survey

Full table: §Appendix A. Key findings:

- **The while-loop is universal.** Every harness implements the same loop: call model → check tool-use → execute → feed result back → repeat. Variations are in what wraps the loop.
- **Tool dispatch splits into two camps:** native-schema (OpenAI/Claude tool-use JSON) vs protocol (MCP, XML-in-system-prompt, Agent-Computer Interface). **MCP has emerged as the cross-harness interoperability standard** (Cline, Goose, Claude Agent SDK, OpenHands all speak MCP).
- **Multi-model support = LiteLLM substrate.** Aider, SWE-agent, OpenAI Agents SDK all delegate to LiteLLM for the N-provider problem. Reinventing that is industry anti-pattern. Strix (the pen-test tool we ran last session) already uses LiteLLM. **Decision: LiteLLM is a fixed dependency, never something to reimplement.**
- **Checkpointing separates research from production harnesses.** LangGraph's per-step checkpointing is best-in-class; Claude Agent SDK does session-level resume via JSONL replay; Aider treats git commits as implicit checkpoints; Cline/Goose have none.
- **Plugin surfaces = hooks | graph-nodes | MCP-servers.** MCP is the winner for cross-tool interop.

**Top 3 prior-art references:**
1. **LiteLLM** — substrate, not prior art to copy. Fixed dependency.
2. **SWE-agent** — cleanest "Agent-Computer Interface" abstraction. Separates agent / environment / models. Mini-SWE-agent is 100 lines and scores >74% SWE-bench. Directly instructive.
3. **OpenAI Agents SDK** — best model for public API shape (Agents + Handoffs + Guardrails + Session). Study its `ModelProvider` protocol.

---

## 3. Strategic options — pick one

### Option A: Plugin-for-any-harness (LIBRARY-FIRST)

Users wire `ctx` into *their* harness (Claude Code today, Aider/Goose/Cline/their-custom-loop tomorrow).

**What ships:**
- Stable Python API: `from ctx.core import bundle_for_query, resolve_skills, install_entity` etc.
- Stable CLI surface unchanged (`ctx-skill-install`, `ctx-mcp-install`)
- One MCP server (`ctx-mcp-server`) that exposes `ctx.recommend_bundle(query)` / `ctx.install(slug)` / `ctx.graph_query(seeds)` as MCP tools — any MCP-speaking harness can call it
- Claude Code integration stays as today; it's just one more MCP consumer

**What does NOT ship:**
- No agent loop
- No provider adapter
- No model calls from ctx code

**Pros:** Tiny delta. No competition with existing harnesses. Plays the MCP interop card. ctx stays a library, which is its actual differentiator.
**Cons:** Users still need their own harness. Doesn't answer "run ctx around MiniMax" — it answers "run MiniMax inside a harness that happens to use ctx's MCP server."

### Option B: Full harness (HARNESS-FIRST)

We write a full harness — while-loop, provider abstraction, tool dispatch, context compaction, checkpointing, multi-agent (Planner/Generator/Evaluator).

**Pros:** User can `ctx run --provider=openai --model=gpt-5.5 "build me a todo app"` and actually get autonomous behavior.
**Cons:** We'd be building a worse SWE-agent / worse OpenAI Agents SDK / worse LangGraph. Our differentiator (alive skills + graph + quality) gets buried under undifferentiated agent-loop code. Huge surface to maintain. We've never built a production harness.

### Option C: Hybrid — minimal harness + plugin retained (RECOMMENDED)

Thin harness layer: LiteLLM-backed provider shim + bare while-loop + MCP-based tool dispatch. Reuses **ctx-core** (graph/quality/resolve/wiki — already provider-agnostic) as its recommendation layer. Claude Code integration becomes one adapter among N.

**What ships (minimum viable harness):**
```
ctx run --provider openai --model gpt-5.5 \
        --mcp ctx,filesystem,github \
        --task "fix the failing tests in this repo"
```

Internally:
1. LiteLLM routes calls to the chosen provider.
2. ctx-core resolves the task → suggests a skill/agent/MCP bundle → auto-attaches relevant MCPs.
3. Thin while-loop: call model, check tool-use, dispatch via MCP, feed back.
4. State: append-only JSONL session file (same pattern as Claude Agent SDK). Context compaction via OpenAI Agents SDK's pattern or direct `litellm.completion()` with summarization.
5. Checkpointing is optional — start without, add LangGraph-style later if users need it.

**What does NOT ship in v1:**
- Planner/Generator/Evaluator split (we defer Anthropic's pattern until there's demand — it's load-bearing in the Anthropic article specifically for *product-build* tasks, not general coding)
- Interactive Playwright testing
- Time-travel debugging

**Pros:** 
- Actually delivers "run around any model"
- Leverages the existing ctx-core (which is already model-agnostic — per §4 inventory, 4 modules are ready today)
- LiteLLM does 90% of the provider-adapter work
- Minimum-surface harness — small enough to maintain while we focus on the skill/graph differentiator
- Path to (A) if we decide we don't want the harness — just stop shipping `ctx run` and keep the MCP server

**Cons:**
- More code than Option A
- We own the loop, so bugs in context compaction / error recovery are ours
- Per the Anthropic article, *our harness encodes our assumptions about model limits* — we have to revisit as models improve

### Recommendation: **C**

The decision fork is really "do we ship a runnable harness, or just a library." If the user's end-state is "wrap MiniMax / GPT-5.5 with ctx," option A requires the user to bring their own loop, which means they need another tool. Option C delivers the end-state with minimum new surface, and keeps A's escape hatch (the MCP server is still useful to other harnesses).

---

## 4. Current coupling inventory (what has to move)

From the CC-specific audit: **~46 references across 12 modules** need to change. Categorized:

### MUST refactor (coupled to Claude Code specifically)

| Module | What's coupled | Refactor |
|---|---|---|
| `ctx_config.py` | Path defaults → `~/.claude/*` | Config becomes per-adapter; adapter supplies its own paths |
| `bundle_orchestrator.py` | Emits CC `hookSpecificOutput` JSON | Output becomes adapter-provided (CC adapter keeps today's shape; generic adapter emits plain text / structured JSON for harness consumption) |
| `context_monitor.py` | Writes `~/.claude/pending-skills.json` | Same — adapter decides the sink path |
| `mcp_install.py` | `subprocess.run(["claude", "mcp", ...])` | Split: `mcp_install_core` (wiki state + manifest) vs `mcp_install_claude` (CC shell-out). Generic adapter writes MCP config to an adapter-owned `.mcp.json` or sends SIGHUP to the host harness. |
| `inject_hooks.py` | Writes CC `~/.claude/settings.json` | Adapter-specific — CC adapter only |
| `skill_loader.py`, `skill_health.py` | CC skill-auto-load assumption | Split loader body from CC-specific launch |
| `skill_suggest.py` (shim) | PostToolUse → bundle_orchestrator | Adapter-specific |

### Already provider-agnostic (**no refactor**)

| Module | Why it's portable |
|---|---|
| `resolve_graph.py`, `semantic_edges.py` | Pure NetworkX + numpy. No LLM dependency. |
| `resolve_skills.py` | Pure ranking. Uses tags/signals but those come from the query, not from CC. |
| `quality_signals.py`, `skill_quality.py`, `mcp_quality.py` | Pure heuristic scoring. |
| `wiki_utils.py`, `wiki_sync.py`, `wiki_graphify.py`, `wiki_query.py` | Generic YAML frontmatter + file layout. Wiki *root path* comes from config, not CC. |
| `catalog_builder.py` | Reads filesystem, writes markdown. No coupling. |

**Net: the model-agnostic core is ~60% of the codebase today.** The adapter work is the remaining ~40%.

---

## 5. Target architecture

```
src/ctx/
├── __init__.py                       # public API re-exports
│
├── core/                             # provider-agnostic business logic
│   ├── graph/                        # resolve_graph + semantic_edges
│   ├── quality/                      # quality_signals + skill/mcp_quality
│   ├── wiki/                         # wiki_{sync,graphify,query,utils}
│   ├── resolve/                      # resolve_skills + stack_skill_map
│   └── bundle/                       # bundle_orchestrator (adapter-neutral part)
│
├── adapters/                         # per-host integrations
│   ├── claude_code/                  # TODAY's integration
│   │   ├── hooks/                    # post_tool_use.py, stop.py, etc.
│   │   ├── install/                  # skill_install, agent_install, mcp_install (CC branch)
│   │   ├── inject.py                 # inject_hooks equivalent
│   │   └── adapter.py                # declares how CC speaks to ctx-core
│   │
│   └── generic/                      # NEW — model-agnostic harness
│       ├── loop.py                   # the while-loop
│       ├── providers/                # LiteLLM wrapper + per-provider quirks
│       │   ├── litellm_provider.py
│       │   └── direct_anthropic.py   # optional bypass for tool-use fidelity
│       ├── tools/
│       │   ├── mcp_router.py         # MCP-first tool dispatch
│       │   └── registry.py
│       ├── state.py                  # JSONL session, resume, no checkpoint yet
│       ├── context.py                # compaction (simple summary strategy v1)
│       └── adapter.py                # declares how the generic harness speaks to ctx-core
│
├── cli/                              # all user-facing CLIs
│   ├── skill_install.py              # ctx-skill-install (wraps adapter)
│   ├── agent_install.py              # ctx-agent-install
│   ├── mcp_install.py                # ctx-mcp-install
│   ├── run.py                        # NEW: ctx run --provider=... --model=...
│   ├── graphify.py                   # ctx-wiki-graphify
│   └── ...
│
├── mcp_server/                       # NEW: expose ctx-core over MCP
│   └── server.py                     # ctx-mcp-server → tools: recommend_bundle, install, graph_query
│
└── utils/                            # _safe_name, _fs_utils, _file_lock
```

### Data flow — generic harness

```
  user query
      │
      ▼
  ctx/cli/run.py ──────────────────────────── (resolve provider from --provider flag)
      │
      ▼
  ctx/adapters/generic/loop.py
      │
      ├──► ctx/core/resolve (pick top-K skill/agent/MCP bundle)
      │    └──► ctx/core/graph + ctx/core/quality (provider-agnostic)
      │
      ├──► ctx/adapters/generic/providers/litellm_provider (model call)
      │    └──► openai / minimax / gemini / anthropic / ollama / …
      │
      ├──► ctx/adapters/generic/tools/mcp_router (tool dispatch)
      │    └──► attached MCP servers (including ctx's own mcp_server!)
      │
      └──► ctx/adapters/generic/state (append session JSONL)
```

### Data flow — Claude Code (unchanged UX)

```
  CC hook fires
      │
      ▼
  ctx/adapters/claude_code/hooks/post_tool_use.py
      │
      ├──► ctx/core/resolve (same code path)
      ├──► ctx/core/bundle (same)
      └──► emit CC hookSpecificOutput JSON (adapter-specific)
```

Both flows share `core/`. The adapter folders own only the thin edges.

---

## 6. Repo reorganization — phased migration

Per CLAUDE.md phased-execution rule: **max 5 files per phase**, each phase ships green before the next starts.

### Phase R0 — scaffolding only (no code moves)
- Create `src/ctx/__init__.py`, `src/ctx/core/`, `src/ctx/adapters/`, `src/ctx/cli/`, `src/ctx/utils/` (empty dirs + `__init__.py`)
- Update `pyproject.toml` to declare `packages = ["ctx", "ctx.core", ...]` alongside existing flat `py-modules` (transitional — both work)
- Add `docs/plans/001-model-agnostic-harness.md` (this file)
- Files touched: ~8 (all new)
- Verification: full suite still 2,654 passing

### Phase R1 — move utilities
- Move `_safe_name.py`, `_fs_utils.py`, `_file_lock.py` → `src/ctx/utils/`
- Update imports in every dependent module to `from ctx.utils import ...`
- Keep legacy shim: `src/_safe_name.py` → `from ctx.utils._safe_name import *` (deprecation)
- Files touched: 3 source + ~15 import updates across the codebase
- Verification: suite green

### Phase R2 — move graph/quality/resolve core (already pure)
- Move `resolve_graph.py`, `resolve_skills.py`, `semantic_edges.py`, `stack_skill_map.py`, `quality_signals.py` → `src/ctx/core/`
- Files touched: 5
- Verification: suite green

### Phase R3 — move wiki
- Move `wiki_utils.py`, `wiki_sync.py`, `wiki_graphify.py`, `wiki_query.py`, `wiki_lint.py` → `src/ctx/core/wiki/`
- Files touched: 5

### Phase R4 — move Claude Code adapter code
- Move `mcp_install.py`, `skill_install.py`, `agent_install.py` → `src/ctx/adapters/claude_code/install/`
- Move `bundle_orchestrator.py`, `skill_suggest.py`, `context_monitor.py` → `src/ctx/adapters/claude_code/hooks/`
- Move `inject_hooks.py` → `src/ctx/adapters/claude_code/`
- Files touched: ~8 — **exceeds 5-file phase rule, split across R4a + R4b**

### Phase R5 — CLI entrypoints
- Thin wrappers under `src/ctx/cli/` — each is ~10 lines re-exporting from its adapter
- Update `pyproject.toml` scripts section
- Files touched: ~6

### Phase R6 — tests mirror the package tree
- Move `src/tests/test_resolve_graph*.py` → `tests/core/test_resolve_graph*.py`
- And so on
- Largest phase but mechanical. Split if necessary.

After R6: drop legacy shims, drop flat `py-modules` from pyproject.toml, single `packages = ["ctx"]`.

### Git hygiene per phase
- Each phase = one commit with test verification
- Use `git mv` so history follows the files (not `delete+add`)
- Conventional commit prefixes: `refactor(repo): phase R1 — move utilities to ctx.utils`
- Don't mix R-phases with feature commits. Freeze feature work during the R-sequence or branch.

---

## 7. Harness work — phased after repo is reorganized

(All phases below assume repo is post-R6.)

### Phase H1 — provider adapter skeleton
- `src/ctx/adapters/generic/providers/litellm_provider.py`: thin wrapper with `complete(messages, tools=None, **kwargs)` returning a provider-normalized response shape
- Config: `~/.ctx/harness-config.json` with `{provider, model, api_key_env, base_url}` — NOT in `~/.claude/`
- Test: mock LiteLLM, exercise the four main providers (openai, anthropic, gemini, ollama) with fake responses
- Files touched: 3
- Success criteria: `python -c "from ctx.adapters.generic.providers import complete; print(complete([{'role':'user','content':'hi'}]))"` returns a structured response

### Phase H2 — MCP router
- `src/ctx/adapters/generic/tools/mcp_router.py`: given a list of MCP server configs, spawn them as subprocesses + maintain stdio streams + route tool calls
- Reuses `claude mcp` schema (MCP is the protocol, not the host)
- Test: spawn a mock MCP server (fs stub), confirm tool list + tool call + tool result round-trip
- Files touched: 4
- Success criteria: `mcp_router.list_tools(["filesystem"])` returns the server's tool schema

### Phase H3 — the while-loop
- `src/ctx/adapters/generic/loop.py`: read query, call provider, check for tool_use, dispatch via mcp_router, append to messages, repeat until no tool_use
- Stop conditions: model returns no tool_use | max_iterations | cost_budget_exceeded | user abort
- Test: fake provider that returns 2 tool calls then a plain response; confirm loop runs 3 model calls + 2 tool dispatches
- Files touched: 3
- Success criteria: a minimal demo task works end-to-end against openai + anthropic + ollama

### Phase H4 — state & session
- Append-only JSONL per session at `<ctx_dir>/sessions/<session_id>.jsonl`
- Resume: `ctx run --resume <session_id>` replays the JSONL and continues
- Files touched: 2

### Phase H5 — context compaction (v1: summarize-on-overflow)
- When next call would exceed provider's context window, summarize all but last N messages via a separate call
- Keep strategy simple; defer LangGraph-style per-step checkpointing
- Files touched: 1

### Phase H6 — integrate ctx-core
- Harness auto-attaches ctx's own MCP server (`ctx-mcp-server`) at startup
- Every turn, expose `recommend_bundle(query)` / `graph_query(seeds)` / `install(slug)` to the model
- The skill/graph/quality system is now usable FROM the harness
- Files touched: 2

### Phase H7 — CLI UX
- `ctx run --provider --model --task --mcp` (comma-list)
- `ctx run --resume`
- `ctx run --list-sessions`
- Files touched: 1

### Phase H8 — ctx MCP server (ships independently too)
- `src/ctx/mcp_server/server.py`: exposes ctx-core as an MCP server any MCP-speaking harness can attach
- This is the Option A deliverable; option C ships it anyway because our own harness uses it
- Files touched: 2

### Phase H9 — adapters for specific hosts (as demand arises)
- Aider: shell-out or import adapter
- Goose: MCP server is enough (Goose speaks MCP)
- Cline: MCP server is enough
- Custom: documented public API in `ctx/__init__.py`

### ── SHIP v1 ──

At this point `ctx run --provider openrouter --model minimax/minimax-m1 --task ...` works end-to-end with a solo agent. Measure failure modes on real tasks before adding P/G/E scaffolding.

### Phase H10 — Planner agent (evidence-driven)
**Trigger:** solo-agent runs consistently fail on tasks that need multi-step decomposition (spec-to-implementation gap, scope drift, planner-less overruns).
- Separate Planner agent that expands vague prompts into structured specs (Anthropic pattern §5: "Specification Artifact")
- Planner → Generator handoff via file-based artifacts (not inline messages)
- `ctx run --planner` flag opts in; default stays solo
- Files touched: ~3

### Phase H11 — Evaluator agent (evidence-driven)
**Trigger:** self-evaluation bias observed in H1-H10 runs — generator declares success on outputs a human would reject.
- Separate Evaluator agent with explicit grading criteria + few-shot calibration
- Feedback loop drives regeneration
- `ctx run --evaluator` flag opts in (usually with `--planner` together)
- Files touched: ~3

### Phase H12 — Sprint contracts
**Trigger:** Evaluator and Generator disagree on "done" criteria in practice.
- Contract artifact agreed before implementation
- Hard pass/fail thresholds per criterion
- Files touched: ~2

### ── SHIP v2 (full three-agent harness) ──

Match Anthropic's reference pattern. Expect 3-5x token cost vs solo. Worth it for high-stakes autonomous runs; overkill for simple edit-a-file tasks. `ctx run --mode=solo|triad` flag exposes both.

---

## 8. Risks & open questions

### Risks

| Risk | Mitigation |
|---|---|
| Repo reorg breaks downstream import paths | Phase R1-R6 with deprecation shims; 6-month shim deprecation window |
| LiteLLM has a bug for the user's target provider (e.g., MiniMax) | Factor providers behind a `ModelProvider` protocol so direct-SDK bypass is trivial; LiteLLM is the default but not a hard dep |
| Harness loop divergence — we own bugs CC used to hide | Keep loop minimal (H1-H3 ship as ~300 LOC). Study SWE-agent's loop as a known-good reference |
| Context compaction is hard and model-specific | Ship simple summarize-on-overflow in H5; swap for better strategy later. Don't block the release |
| "Alive skill" semantics may not map to harnesses that don't auto-load | Our MCP server exposes recommendation as a tool; model explicitly asks for skills. Harness-agnostic. |
| Self-evaluation anti-pattern (Anthropic §4) if users wire ctx's quality scorer as grader for its own output | Document that quality signals are for *discovery*, not for grading agent outputs |
| Strix-style scanning of the new harness surface | Every new adapter module ships with security tests (no-test-no-merge already enforces this) |

### Open questions — need user decision

1. **Which option?** A (library + MCP server only), B (full harness), or C (minimal harness + MCP server + CC adapter)? My recommendation: C.
2. **Repo reorg scope** — all phases R0-R6 as proposed, or stop after R3 (core only)?
3. **Which providers ship in H1?** LiteLLM gives us ~100 for free, but which 3-5 are the "tier-1 tested" set? MiniMax + GPT-5.5 + Anthropic + Ollama + (?)
4. **Is `~/.ctx/` the right new config root**, or do we keep everything under the adapter-owned directory (`~/.claude/` for CC adapter, `~/.ctx/` for generic)?
5. **Do we want the Planner/Generator/Evaluator split (Anthropic's recommended pattern) in v1**, or defer to a later phase? My recommendation: defer — it's load-bearing for *product builds* specifically, not for general agentic coding tasks.
6. **Checkpointing — LangGraph-style per-step, or session-level JSONL replay only (Claude Agent SDK pattern)?** v1 = JSONL replay; revisit if users ask for time-travel.
7. **License / distribution** — publish as a pip package (`pip install ctx-harness` or similar) after the reorg? Repo is already clean enough if we want to.

---

## 9. Non-goals

- We are **not** replacing Claude Code as a harness. The CC integration stays first-class.
- We are **not** competing with SWE-agent / OpenAI Agents SDK / LangGraph on general-purpose agent features. Our differentiator is the **alive skill / knowledge graph / quality scoring** system, not the while-loop.
- We are **not** going to reinvent LiteLLM. It's a fixed dependency for provider abstraction.
- We are **not** reimplementing MCP. It's the protocol, not something we own.
- We are **not** building Planner/Generator/Evaluator in v1 (defer; Anthropic's pattern is load-bearing for product-build tasks specifically).

---

## 10. Success criteria

**Minimum viable** (end of Phase H7):
```bash
ctx run --provider openai --model gpt-5.5 --mcp ctx,filesystem \
    --task "find the failing tests in this repo and fix them"
```
- Runs to completion or max-iterations without crashing
- Cost tracked + shown
- Session JSONL persisted; resumable via `--resume`
- ctx's own skill-recommendation MCP server gets consulted at least once (evidence in the JSONL)
- Same task against `--provider anthropic --model claude-opus-4-7` works identically (byte-diff on artefact level, not prose)

**Full delivery** (end of Phase H9):
- 3 adapters ship: claude_code (existing), generic (new), aider (new)
- `ctx-mcp-server` is published + a Cline/Goose recipe documents attaching it
- Coverage floor on new code: 80%+ (current floor is 40%; ratchet up)
- Full docs under `docs/harness/`

---

## Appendix A — harness comparison table

(See `docs/plans/001-research-appendix.md` for the full survey with citations. Summary inline §2.)

## Appendix B — detailed file-move map for each R-phase

(To be filled in as each R-phase ships. R0 scaffolding only — no files move.)
