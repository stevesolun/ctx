# Review heuristics

Use these heuristics when the diff raises design questions that repository
standards and deterministic tools do not settle. Repository conventions take
precedence. Label these as judgment calls, not hard violations.

## Common design smells

- **Mysterious name:** A name does not reveal what a value or operation means.
- **Duplicated code:** The same logic shape appears in multiple changed sites.
- **Feature envy:** A method depends more on another object's data than its own.
- **Data clumps:** The same fields or parameters repeatedly travel together.
- **Primitive obsession:** A primitive stands in for a meaningful domain type.
- **Repeated conditionals:** Multiple sites branch on the same type or state.
- **Shotgun surgery:** One behavior change requires scattered edits.
- **Divergent change:** A module changes for several unrelated reasons.
- **Speculative generality:** An abstraction serves no current requirement.
- **Message chains:** A caller depends on a long navigation path.
- **Middle man:** A layer mostly delegates without adding a useful boundary.
- **Refused bequest:** An implementation inherits behavior it mostly rejects.

Prefer the smallest correction that improves the changed behavior. Do not turn a
review into an unrelated redesign, and do not repeat findings already decided by
formatters, linters, type checkers, or tests.

## Spec questions

Check for:

- requested behavior that is missing or only partially implemented;
- behavior that contradicts the source requirement;
- added scope without a clear requirement;
- success paths that work while relevant failures or edge cases do not; and
- implementation details that make a stated requirement untestable.

Tie each finding to the source requirement when one exists. If no spec is
available, describe the resulting limit rather than inferring requirements.
