---
name: codebase-design
description: Design or improve module interfaces using deep-module heuristics. Use when deciding where a seam belongs, reducing caller-facing complexity, improving testability or navigability, or comparing module designs.
---

# Codebase Design

Seek **deep modules**: substantial useful behavior behind a small, stable
interface. Use the concepts below as design lenses, while matching the
repository's established domain language.

## Working vocabulary

- **Module**: a coherent unit with an interface and implementation, at any scale.
- **Interface**: everything callers must know, including invariants and failure
  modes—not just a type signature.
- **Seam**: a location where behavior can vary without editing its callers.
- **Adapter**: an implementation that connects at a seam.
- **Depth**: useful capability relative to interface complexity.
- **Leverage**: capability gained by callers; **locality**: change and knowledge
  concentrated for maintainers.

Translate these terms to the project's vocabulary when that makes the design
clearer. Consistency with the codebase is more valuable than enforcing a private
lexicon.

## Design workflow

1. Map representative callers, responsibilities, dependencies, and existing
   contracts.
2. Look for coordination or policy that can move behind a smaller interface.
3. Place seams where variation, ownership, or test isolation justifies them.
4. Compare alternatives when the choice is consequential.
5. Validate the candidate with representative caller code and tests.

## Heuristics, not laws

- Reduce methods, parameters, ordering constraints, and configuration callers
  must understand.
- Use the deletion test: if removing a module merely spreads its complexity
  across callers, it was likely earning its keep.
- Treat pass-through layers skeptically, but keep them when they provide a real
  compatibility, ownership, policy, or navigation boundary.
- Introduce dependency seams when actual variation or test isolation repays the
  indirection; avoid interfaces justified only by hypothetical futures.
- Prefer tests through observable interfaces, while allowing focused internal
  tests when they provide cheaper or more precise feedback.
- Favor explicit dependencies and returned results when they improve control and
  testability; side effects and internally created dependencies can still be
  appropriate at well-defined boundaries.

Read [DEEPENING.md](DEEPENING.md) when consolidating a cluster across dependency
boundaries. Read [DESIGN-IT-TWICE.md](DESIGN-IT-TWICE.md) when materially
different interface designs can be explored independently.
