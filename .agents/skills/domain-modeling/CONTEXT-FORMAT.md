# Domain context format

Follow the repository's existing glossary or context-map convention. When none
exists and a new artifact is useful, this is a compact starting point.

```markdown
# Context name

One or two sentences describing the domain context and why it exists.

## Language

**Canonical term:** A concise, project-specific definition.
_Avoid:_ Ambiguous or deprecated synonyms, when naming them prevents confusion.
```

Include only concepts that carry special meaning in this domain. Define what a
term is and how it differs from nearby concepts; keep implementation details in
specs, code, or ADRs.

For repositories with several bounded contexts, a root context map may list
each context, its location, and important relationships:

```markdown
# Context map

- **Ordering:** Receives and tracks customer orders.
- **Billing:** Generates invoices and processes payment.

## Relationships

- Ordering emits `OrderPlaced`; Fulfillment consumes it.
```

Create or split context files only when the model actually has distinct
boundaries and the artifact will be maintained. Do not force a multi-context
layout from directory structure alone.
