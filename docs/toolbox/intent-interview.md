# Intent interview

[`src/intent_interview.py`](https://github.com/stevesolun/ctx/blob/main/src/intent_interview.py)
bootstraps your toolbox set via a short, skippable interview.

The slash command `/toolbox init` is a thin wrapper; see
[`.claude/commands/toolbox-init.md`](https://github.com/stevesolun/ctx/blob/main/.claude/commands/toolbox-init.md).

## Flow

1. **Detect repo state** — is this a git repo? Any commits? What languages?
2. **Load behavior profile** — read `~/.claude/user-profile.json` for any
   mined suggestions.
3. **Ask up to three questions**:
   - Which starter toolboxes to activate.
   - Which miner suggestions to accept (if any).
   - Default analysis mode for new toolboxes.
4. **Persist** — write chosen toolboxes to `~/.claude/toolboxes.json`
   (only when `--apply` is passed).

Any prompt can be skipped with the word `skip`.

## Repo state detection

```python
@dataclass(frozen=True)
class RepoState:
    is_git: bool
    commit_count: int
    languages: dict[str, int]     # extension → file count
    markers: dict[str, str]       # marker file → language
    has_toolbox_config: bool

    @property
    def is_blank(self) -> bool:
        # True when the repo has effectively nothing to analyze yet.
        return not self.is_git or self.commit_count == 0 or (
            not self.languages and not self.markers
        )
```

Language scoring uses both extensions (`.py`, `.ts`, …) and marker files
(`pyproject.toml`, `Cargo.toml`, `Dockerfile`, …). Marker files bump the
score by 5 to reflect that they declare intent more strongly than a
stray extension match.

## Usage

```bash
# Default: interactive, dry-run (no write)
python src/intent_interview.py init

# Detect state only
python src/intent_interview.py detect

# Preset flows (no prompts)
python src/intent_interview.py init --preset blank --apply
python src/intent_interview.py init --preset existing --apply
python src/intent_interview.py init --preset docs-heavy --apply
python src/intent_interview.py init --preset security-first --apply

# Fully structured (CI / scripted setup)
python src/intent_interview.py init \
  --non-interactive \
  --starters ship-it,security-sweep \
  --suggestions 1,2 \
  --analysis dynamic \
  --apply
```

## Presets

| Preset | Starters | Default scope |
|---|---|---|
| `blank` | ship-it, security-sweep, fresh-repo-init | dynamic |
| `existing` | ship-it, refactor-safety | dynamic |
| `docs-heavy` | docs-review | diff |
| `security-first` | security-sweep | full |

## Skip semantics

- Typing `skip` at any prompt short-circuits the whole interview: no
  starters activated, no suggestions accepted, analysis mode unchanged.
- `--skip` on the CLI is the non-interactive equivalent.

## Exit codes

- `0` — success; JSON payload printed on stdout.
- non-zero — unrecoverable error (unknown preset, malformed args).

## Related

- [Starter toolboxes](starters.md) — the five bundles the interview can activate.
- [Behavior miner](behavior-miner.md) — source of the suggestions the interview offers.
