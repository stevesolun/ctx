# Stage 4: Check — Validate Before Loading

Binary YES/NO gates. Any NO = stop and report before proceeding.

## Gates

- [ ] **G1** Load list has ≥ 1 skill?
- [ ] **G2** Load list has ≤ 15 skills?
- [ ] **G3** No skill appears in both load AND unload?
- [ ] **G4** All loaded skills have a valid path that exists on disk?
- [ ] **G5** No two conflicting skills in load list? (e.g., flask + fastapi, jest + vitest)
- [ ] **G6** Manifest `generated_at` is within the last 24 hours?

## Failure Handling

| Gate | Failure action |
|------|---------------|
| G1 | Load only meta-skills (skill-router), warn user |
| G2 | Trim to top 15 by priority, warn about excluded skills |
| G3 | Remove the skill from unload (keep loaded version) |
| G4 | Remove missing-path skills from load, add to suggestions |
| G5 | Keep highest-priority conflicting skill, warn about the other |
| G6 | Warn that manifest may be stale, offer to re-scan |

## Pass Condition

All gates YES (or failures handled per table above).
Proceed to Stage 5 only after resolving all failures.
