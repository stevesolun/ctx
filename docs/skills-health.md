# Skill health dashboard

[`src/skill_health.py`](https://github.com/stevesolun/ctx/blob/main/src/skill_health.py)
scans `~/.claude/skills/` and `~/.claude/agents/` for structural and
catalog issues, then produces a JSON or human-readable dashboard. It
also self-heals catalog drift — without ever modifying a SKILL.md.

## What it checks

For each skill (`~/.claude/skills/<name>/SKILL.md`) and each agent
(`~/.claude/agents/<name>.md`):

| Code | Severity | Condition |
|---|---|---|
| `missing-file` | error | Skill directory has no SKILL.md |
| `unreadable` | error | File exists but can't be decoded as UTF-8 |
| `no-frontmatter` | error | Missing or malformed `---` YAML fence |
| `frontmatter-missing-name` | error | Frontmatter has no `name:` field |
| `frontmatter-missing-description` | warning | Missing `description:` (router relevance suffers) |
| `empty-body` | error | Fewer than `min_body_lines` non-blank lines |
| `over-threshold` | warning | Line count exceeds `line_threshold` (default 180) |

## Drift detection

`DriftReport` cross-references three sources:

- on-disk entities (skills + agents),
- `~/.claude/skill-manifest.json` → `load[].skill` entries,
- `~/.claude/pending-skills.json` → `graph_suggestions[].name` and
  `unmatched_signals[]`.

Anything in the manifest or pending file that doesn't exist on disk
becomes an *orphan*. Orphans are the only thing `heal` is allowed to
touch.

## Self-healing

```bash
python src/skill_health.py heal
```

- drops orphaned entries from `skill-manifest.json`
- drops orphaned entries from `pending-skills.json`
- writes atomically (`tempfile.mkstemp` + `os.replace`)
- never modifies SKILL.md files or agent .md files

If nothing needs healing, prints `[heal] nothing to do.` and exits 0.

## CLI

```bash
# Emit a full JSON report
python src/skill_health.py scan

# Pretty dashboard
python src/skill_health.py dashboard

# CI gate: exit 2 if any error-severity issue or drift is present
python src/skill_health.py check --strict

# Apply safe autofixes to manifest + pending
python src/skill_health.py heal
```

## Data model

```python
@dataclass(frozen=True)
class Issue:
    code: str
    severity: str      # "warning" | "error"
    message: str

@dataclass(frozen=True)
class EntityHealth:
    name: str
    kind: str          # "skill" | "agent"
    path: str
    lines: int
    has_frontmatter: bool
    issues: tuple[Issue, ...] = ()

@dataclass(frozen=True)
class DriftReport:
    orphaned_manifest: tuple[str, ...] = ()
    orphaned_pending: tuple[str, ...] = ()

@dataclass(frozen=True)
class HealthReport:
    generated_at: float
    entities: tuple[EntityHealth, ...]
    drift: DriftReport
    totals: dict[str, int]
```

`HealthReport.has_errors` is true when any entity has severity `error`
*or* drift is non-empty — that's the single predicate behind
`check --strict`'s exit code.

## Related

- [Memory anchoring](memory-anchor.md) — dead-reference detection for
  auto-memory notes.
