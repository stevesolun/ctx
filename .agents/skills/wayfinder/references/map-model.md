# Wayfinder map model

Adapt this model to the repository's tracker rather than forcing one platform
shape.

## Core concepts

- **Destination:** the state that makes the route complete.
- **Decision:** a settled answer recorded in one authoritative place.
- **Question:** a precise unknown that can be investigated now.
- **Fog:** an in-scope area that cannot yet be stated as a precise question.
- **Out of scope:** work deliberately outside the current destination.
- **Dependency:** a question whose answer is needed before another can advance.
- **Frontier:** open, unblocked, unclaimed questions that can usefully advance.

## Minimal map

```markdown
## Destination

<What reaching the end means>

## Decisions

- <Decision and link to its evidence>

## Open questions

- <Question, owner, dependencies, and evidence needed>

## Fog

- <Area to revisit after a named dependency is resolved>

## Out of scope

- <Boundary and reason>
```

Use native tracker relationships when they improve querying or visibility.
Otherwise, use explicit links and dependency fields. Keep the map an index;
store detailed evidence with the decision or question that owns it.
