# Memory anchoring

[`src/memory_anchor.py`](https://github.com/stevesolun/ctx/blob/main/src/memory_anchor.py)
walks the auto-memory store and flags references that no longer
resolve against the current repository.

## Why it exists

Claude's auto-memory accumulates notes that look like:

> Fixed the `add_skill()` bug in `src/skill_loader.py:42`. See also
> `docs/intent-interview.md`.

Those backtick references rot as the codebase moves. A renamed file, a
deleted module, a moved doc — and the memory silently points at
nothing. `memory_anchor` turns that silent rot into a loud dashboard.

## Where it looks

Memory files live under:

```text
~/.claude/projects/<slug>/memory/*.md
```

The module recursively scans that tree. You can override the root with
`--memory-root` (useful for tests or multi-project setups).

## What counts as a reference

Only tokens inside **backtick code spans** qualify. The heuristic is
deliberately conservative to keep false positives low:

- known extension (`.py`, `.md`, `.json`, `.yml`, `.ts`, `.rs`, …), or
- contains a `/` with a dotted final segment.

Tokens with whitespace, `()` suffixes, `http(s)://` prefixes, or leading
`-` are rejected up front. A trailing `:<digits>` is parsed as a line
suffix — `:` without digits is preserved (keeps Windows drive letters
intact).

## How resolution works

For each extracted reference, the module asks whether it resolves:

1. tilde-expand, if `~/…`
2. if absolute, does the path exist?
3. does `repo_root / path` exist?
4. does `repo_root / src / path` exist?

If any candidate hits, the ref is *live*; otherwise *dead*.

## CLI

```bash
# JSON report for downstream tooling
python src/memory_anchor.py scan

# Human dashboard
python src/memory_anchor.py dashboard

# CI gate: exit 2 if any dead references remain
python src/memory_anchor.py check --strict

# Override repo / memory roots
python src/memory_anchor.py check --strict \
  --repo-root /path/to/repo \
  --memory-root /path/to/project/memory
```

When `--repo-root` is omitted, the module walks upward from the current
directory to the nearest `.git/` ancestor.

## Data model

```python
@dataclass(frozen=True)
class AnchorRef:
    raw: str          # exactly the backtick contents
    path: str         # path sans trailing :<line>
    line: int | None
    exists: bool

@dataclass(frozen=True)
class MemoryAnchorFile:
    memory_path: str
    refs: tuple[AnchorRef, ...]
    # derived: .live, .dead

@dataclass(frozen=True)
class AnchorReport:
    generated_at: float
    repo_root: str
    memory_root: str
    files: tuple[MemoryAnchorFile, ...]
    # derived: .all_refs, .live_count, .dead_count, .has_dead
```

## Related

- [Skill health dashboard](skills-health.md) — structural and drift
  checks for the skill + agent catalog.
