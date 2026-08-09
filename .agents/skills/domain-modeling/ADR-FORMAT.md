# ADR format

Follow the repository's existing ADR location, numbering, and status
conventions. When none exist, a decision can be recorded minimally:

```markdown
# Short decision title

Describe the context, the decision, and why this option was chosen.
```

Add sections such as status, considered options, consequences, or supersession
only when they preserve useful information.

An ADR is most valuable when the decision is:

- costly to reverse;
- surprising without historical context; and
- the result of a real tradeoff.

Good candidates include system boundaries, integration patterns, technology
choices with meaningful lock-in, non-obvious constraints, and deliberate
deviations from the expected design. Routine implementation choices and
temporary priorities usually do not need an ADR.
