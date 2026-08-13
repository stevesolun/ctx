# Logic Prototype

A logic prototype makes business rules, state transitions, or data-shape
trade-offs concrete enough to exercise.

## Workflow

1. Write down the question and what evidence would answer it.
2. Choose the cheapest useful interaction:
   - a small terminal UI for exploratory state transitions;
   - a script or table of cases for deterministic rules;
   - a focused test harness for invariants or regression behavior;
   - a notebook or REPL when rapid data-shape exploration matters.
3. Use the project's existing runtime and tooling where practical.
4. Keep the logic separate from the harness when that separation helps compare
   designs or reuse the result. Do not turn a disposable experiment into a
   premature production abstraction.
5. Exercise representative normal, edge, and invalid cases. Surface the state,
   transition, output, or error that answers the question.
6. Give the user the run command and summarize what the prototype demonstrated.

## Interactive terminal shape

When manual state exploration is the useful feedback loop, build a lightweight
terminal interface:

- Show the current relevant state and available actions.
- Accept one action at a time and render the resulting state.
- Keep the frame compact and make invalid transitions visible.
- Use native terminal capabilities or dependencies already present in the
  project before adding a UI library.

## Guardrails

- Avoid real production mutations unless they are essential to the question and
  safely isolated.
- Avoid speculative generalization.
- Keep harness concerns from obscuring the logic under evaluation.
- Treat prototype code as evidence, not production-ready implementation.
