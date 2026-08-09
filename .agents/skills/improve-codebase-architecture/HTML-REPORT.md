# Architecture report guide

Use a visual report when relationships, before/after structure, or several
candidates would be harder to compare in prose. Follow the user's requested
destination and the environment's artifact conventions.

## Suggested structure

- Repository or subsystem and the scope inspected.
- Candidate cards with evidence and affected modules.
- A recommendation strength such as `Strong`, `Worth exploring`, or
  `Speculative`.
- One top recommendation with its main tradeoff.
- Limits: relevant areas or evidence not inspected.

Each candidate can include:

- **Observed friction:** what is difficult today.
- **Proposed deepening:** which responsibility moves behind which interface.
- **Evidence:** concrete call paths, change patterns, tests, or dependencies.
- **Benefits:** expected locality, leverage, or test improvement.
- **Costs:** migration work, compatibility risk, or added indirection.
- **Decision conflicts:** ADRs or constraints that would need reconsideration.

## Visual choices

Choose the smallest visual that makes the relationship clearer:

- A dependency or call-flow graph for tangled relationships.
- A sequence diagram for excess round trips.
- A layered cross-section for shallow pass-through modules.
- Interface-versus-implementation mass for module depth.
- A before/after call-graph collapse for consolidation.

Mermaid is useful for graph-shaped relationships. HTML/CSS or SVG may be better
for editorial comparisons. Prefer self-contained output; use external CDNs only
when the environment permits them.

Keep labels close to the repository's domain and architecture language, but
favor clarity over enforcing a special vocabulary. Diagrams should carry useful
evidence rather than decorate the report.
