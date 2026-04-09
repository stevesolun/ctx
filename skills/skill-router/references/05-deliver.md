# Stage 5: Deliver — Present to User

Show a concise summary. Keep it under 20 lines.

## Format

```
─── Skill Router ──────────────────────────────────
Project: <repo_name> (<project_type>)
Detected: <top 3 stacks with confidence>

Loading  [N]: <skill1>, <skill2>, <skill3> ...
Unloading[M]: <skill_a>, <skill_b> ... (first 5)

⚠ Warnings: <if any, one per line>
💡 Suggestions: <missing but needed skills, with install hint>
───────────────────────────────────────────────────
```

## Rules

- List loaded skills in priority order (highest first)
- If unload count > 5: show first 5 then "+ N more"
- Warnings: only show if there are any (no empty section)
- Suggestions: include install hint if skill is in a marketplace

## After Delivery

The session continues normally. The loaded skills are now available
via the Skill tool. The PostToolUse hook watches for new intent signals.

If the user asks to re-evaluate: re-run from Stage 1.
If the user asks to force-load a skill: add `always_load: true` to its wiki page.
If the user asks to block a skill: add `never_load: true` to its wiki page.
