---
name: to-tickets
description: Break a plan, spec, or conversation into dependency-aware, independently verifiable tickets. Use explicitly when the user wants work sliced for local planning or an issue tracker.
---

# Create tickets

Read the source plan, spec, issue, or conversation and inspect enough of the
repository to avoid fictional boundaries. Use the project's established domain
language and preserve relevant architectural decisions.

Prefer narrow vertical slices that deliver observable behavior and can be
verified independently. Size work for the actual team and risk rather than a
fixed context-window rule. Use horizontal or mechanical slices when the change
cannot remain coherent vertically.

Model only real blocking edges. Tickets without blockers form the available
frontier and may proceed concurrently. For a broad compatibility migration,
consider an expand–migrate–contract sequence so intermediate states remain
usable.

Each ticket should communicate:

- the behavior or outcome it delivers;
- concrete acceptance criteria;
- genuine dependencies;
- relevant constraints or source references; and
- scope boundaries when adjacent work is easy to confuse.

Use the [ticket format](TICKET-FORMAT.md) when the destination has no stronger
convention. Avoid brittle line numbers and implementation recipes unless the
ticket is intentionally mechanical.

Present a draft when granularity or dependencies remain uncertain. Publish or
write tickets only when requested and authorized, following the configured
tracker's native dependency model. Do not alter a parent item or apply workflow
labels unless that action is part of the request.
