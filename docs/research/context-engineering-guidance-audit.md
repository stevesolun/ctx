# Context-Engineering Guidance Audit

Status date: 2026-07-30

## Verdict

CTX is now **strongly aligned in practice** with the current Anthropic and
OpenAI guidance reviewed here. There is no published conformance standard, so
“full compliance” would overstate what either vendor claims. The defensible
conclusion is:

- the always-loaded repository context is small and repository-specific;
- conditional workflows are disclosed through focused skills and direct
  references;
- guidance generally sets outcomes, boundaries, and heuristics rather than
  prescribing every step;
- deterministic work, validation, and parallelism are selected
  proportionally; and
- the imported Matt Pocock skills no longer reintroduce a large, rigid
  instruction surface.

No further instruction-file simplification is justified by this document
audit. The remaining work is behavioral evaluation, not more prose.

## Scope and measurements

This audit reread the current `AGENTS.md`, `CLAUDE.md`, both CTX Claude skill
entrypoints, all 22 repository-local Codex `SKILL.md` files, all 22
`agents/openai.yaml` policies, and every file directly linked from those skill
entrypoints.

| Surface | Lines | Words | Bytes | Loading behavior |
|---|---:|---:|---:|---|
| `AGENTS.md` | 24 | 150 | 1,105 | Always loaded by Codex; imported by Claude |
| `CLAUDE.md` | 1 | 1 | 11 | Only `@AGENTS.md` |
| `ctx-dispatch` entrypoint | 18 | 138 | 1,004 | On demand |
| `ctx-verify` entrypoint | 48 | 360 | 2,632 | On demand |
| 22 Codex skill entrypoints | 689 | 4,517 | 31,911 | Name and description first; body on demand |
| 26 direct skill references | 1,058 | 6,577 | 45,471 | Loaded only for the relevant branch |

Of the 22 Codex skills, 13 set `allow_implicit_invocation: false`; nine may be
matched implicitly. Their 22 description lines total 496 words and 3,497
bytes. The nine implicit bodies total 2,139 words. The longest entrypoint is
67 lines, and the largest execution body is 324 words by the root measurement;
branch-specific catalogues, examples, templates, and platform commands now
live in direct references. All 26 direct links resolve.

OpenAI documents that Codex initially receives skill names and descriptions,
then loads the full skill only when selected. It also caps the initial skills
list and recommends concise, bounded descriptions. The current layout uses
that mechanism as intended.
[OpenAI skill guidance](https://developers.openai.com/codex/skills/)

## Anthropic article audit

The comparison below covers the substantive recommendations in Anthropic's
[The new rules of context engineering for Claude 5 generation
models](https://claude.com/blog/the-new-rules-of-context-engineering-for-claude-5-generation-models).
Anthropic reports removing over 80% of Claude Code's system prompt without a
measurable coding-evaluation loss; that result motivates simplification but is
not itself a target percentage for other repositories.

| Article recommendation | Current CTX evidence | Result |
|---|---|---|
| Replace overlapping micro-rules with surrounding-context judgment. | Root context contains no comment, naming, formatting, or implementation micro-rules. Skills use language such as “when useful,” “proportionally,” and “when risk warrants.” Exact constraints remain mainly around destructive or external mutation, tracker state, merge safety, and verification evidence. | **Aligned.** The remaining strictness is bounded to costly or explicit workflows. |
| Replace example-heavy prompting with expressive tool, script, and file interfaces. | Root context points deterministic work toward existing scripts, schemas, tests, and batched tools. Skills reference executable checks and a reusable human-in-the-loop script instead of embedding every operation in startup context. | **Not fully verifiable from instruction files.** The files express the right selection policy, but actual parameter quality, error messages, composability, and task coverage require interface-level tests. |
| Put conditional information behind progressive disclosure. | `AGENTS.md` delegates dispatch and verification to separate skills. The 22 Codex skills defer their bodies, and 26 branch-specific references defer another 6,577 words. | **Aligned.** |
| Avoid a monolithic `CLAUDE.md` or `SKILL.md`; use a tree of files. | `CLAUDE.md` contains only `@AGENTS.md`; the imported map is 21 lines. No skill entrypoint exceeds 67 lines or 324 execution-body words. Longer platform, teaching, debugging, testing, and design detail is one link away. | **Aligned.** |
| State guidance once and keep tool descriptions simple. | Root files name when to load workflows but do not repeat their procedures. Skill descriptions define selection; execution guidance remains in the skill body or a direct reference. | **Aligned for audited files.** Host-provided tool descriptions were outside this repository audit. |
| Use auto-memory rather than accumulating session memory in `CLAUDE.md`. | Neither root file stores history, personal memory, corrections, or session notes. | **Aligned.** |
| Prefer rich references such as code, tests, artifacts, and rubrics over prose-only specs. | `ctx-verify` points to executable scripts and tests and includes a six-part evaluation rubric. Other skills point to code-oriented test examples, shell tooling, tracker interfaces, and artifact formats. | **Aligned.** References are used when they improve fidelity, not required for every task. |
| Keep `CLAUDE.md` lightweight: repository purpose and non-obvious gotchas. | The effective shared map is 150 words: a brief purpose, two migration or validation gotchas, and three workflow pointers. | **Aligned.** |
| Keep skills lightweight, split long detail, and encode product or team opinions. | The 22 adapted skills use concise entrypoints and direct references. They preserve useful opinions—deep-module design, behavior-focused tests, domain language, evidence-backed research—while repeatedly deferring to repository context and proportional judgment. | **Aligned.** |
| Use `/doctor` to help rightsize context. | The current files were measured and manually audited, but a fresh Claude `/doctor` result was not captured here. | **Not verified.** `/doctor` is a diagnostic aid, not a conformance certificate. |

The intentionally stronger language in `ctx-verify`, `triage`, merge-conflict
handling, and the user-triggered `grilling` workflow is not a material conflict
with the article. Anthropic explicitly allows stronger constraints in highly
important areas, and opinionated workflows are a stated purpose of skills. The
important distinction is that these constraints are conditional and narrow,
not ambient coding policy.

## OpenAI compatibility

The same final design matches current first-party OpenAI guidance:

- OpenAI recommends leaner prompts, stating each instruction once, exposing
  only relevant tools, preserving examples only when they encode a requirement
  or fix a measured gap, and retaining domain context, hard constraints,
  approval boundaries, and success criteria. CTX follows that pattern.
  [OpenAI model guidance](https://developers.openai.com/api/docs/guides/latest-model)
- OpenAI describes `AGENTS.md` as a map rather than an encyclopedia and favors
  mechanically enforced invariants with autonomy inside the boundary. CTX's
  21-line map and validation skill are a direct match.
  [OpenAI harness engineering](https://openai.com/index/harness-engineering/)
- OpenAI's skill guidance says to keep one job per skill and prefer
  instructions over scripts unless deterministic behavior or external tooling
  justifies code. CTX's “scripts for deterministic work; LLMs for semantic
  judgment” policy is compatible with this narrower rule.
  [OpenAI skill guidance](https://developers.openai.com/codex/skills/)
- OpenAI recommends parallel agents for independent, especially read-heavy,
  lanes and warns that parallel write-heavy work adds conflicts and
  coordination cost. `ctx-dispatch` makes the same tradeoff: simple work stays
  local, writers get disjoint ownership, and resource inspection is reserved
  for genuinely compute-heavy work.
  [OpenAI subagent guidance](https://developers.openai.com/codex/subagents/)

This means the repository does **not** implement “maximum parallelism,
orchestrator, and validator on every task” literally. That blanket rule would
conflict with the cited guidance. It implements the intended outcome:
parallelism when lanes are independent and worthwhile, one coordinator for
synthesis, deterministic validation proportional to the changed surface, and
an independent semantic validator only when meaningful risk remains.

## Compatible local policies, not Anthropic requirements

These CTX policies are sensible and supported by the broader OpenAI guidance,
but they should not be described as direct requirements of the Anthropic
article:

- prefer scripts, schemas, tests, and batched tools for deterministic work;
- use LLM calls where semantic judgment adds value;
- inspect CPU, memory, and disk pressure before genuinely compute-heavy work;
- parallelize independent lanes while isolating writes; and
- use an orchestrator and independent validator selectively.

Anthropic's article discusses interface design and verifier agents as a use of
rubrics. It does not prescribe general parallelism, hardware inspection, or a
scripts-before-LLMs rule.

## Remaining evidence gaps

1. **Interface quality:** Markdown can show that a script or tool is referenced,
   not that its interface is expressive or reliable. Validate important tools
   with representative inputs, failure cases, and task-level benchmarks.
2. **Trigger quality:** File shape and `openai.yaml` policy are valid, but fresh
   sessions should still test prompts that should and should not invoke each of
   the nine implicit skills. Compare output with a no-skill baseline where the
   skill's value is uncertain.
3. **Claude runtime diagnostics:** A fresh `/doctor` and, if useful, `/context`
   inspection would confirm the host's actual assembled context. Their absence
   does not reveal a file-level conflict; it limits runtime verification.
4. **Host tool descriptions:** This audit did not inspect the complete
   Claude/Codex tool schema supplied outside the repository, so the article's
   “simple tool descriptions” recommendation is only verified for local files.

The next useful step is therefore an evaluation pass on representative tasks
and interfaces. Adding more standing rules would move the repository away from
the guidance it now follows.
