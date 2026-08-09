# Design It Twice

When materially different interfaces are plausible, compare more than one
design. Parallel agents can accelerate independent exploration, but local
sketches are enough for a small problem.

Uses the vocabulary in [SKILL.md](SKILL.md) — **module**, **interface**, **seam**, **adapter**, **leverage**.

## Process

### 1. Frame the problem space

Before exploring alternatives, frame the problem space:

- The constraints any new interface would need to satisfy
- The dependencies it would rely on, and which category they fall into (see [DEEPENING.md](DEEPENING.md))
- A rough illustrative code sketch to ground the constraints — not a proposal, just a way to make the constraints concrete

Share the framing when user feedback would materially improve the design.

### 2. Explore alternatives

Produce two or more meaningfully different interfaces. Use parallel sub-agents
when the alternatives divide cleanly and the expected insight exceeds
coordination cost. Otherwise sketch them locally.

Give each exploration the same constraints and a distinct optimization target,
for example:

- Alternative 1: "Minimize the interface — aim for 1–3 entry points max. Maximise leverage per entry point."
- Alternative 2: "Maximise flexibility — support many use cases and extension."
- Alternative 3: "Optimise for the most common caller — make the default case trivial."
- Alternative 4 (if applicable): "Design around ports and adapters for
  cross-seam dependencies."

Use the project's vocabulary. The terms in [SKILL.md](SKILL.md) are lenses, not a
required replacement for established domain language.

For each alternative, capture:

1. Interface (types, methods, params — plus invariants, ordering, error modes)
2. Usage example showing how callers use it
3. What the implementation hides behind the seam
4. Dependency strategy and adapters (see [DEEPENING.md](DEEPENING.md))
5. Trade-offs — where leverage is high, where it's thin

### 3. Present and compare

Present the designs at a level proportional to the decision. Compare **depth**,
**locality**, **seam placement**, migration cost, and fit with existing code.

Recommend the strongest design and explain its trade-offs. Propose a hybrid only
when it is more coherent than either source design.
