# Skill-design glossary

Use these terms as optional shared vocabulary, not as a required ontology.

## Invocation

- **Explicit invocation:** the user deliberately selects a skill. Prefer it
  when the workflow is rare, consequential, or highly opinionated.
- **Implicit invocation:** the host selects a skill from its description.
  Descriptions should distinguish positive triggers from nearby non-triggers.
- **Context load:** metadata or instructions occupying attention before they
  are relevant.
- **Router:** a small skill that recommends another skill or short sequence.

## Information hierarchy

- **Entrypoint:** `SKILL.md`; contains essential selection and execution
  guidance.
- **Reference:** detail loaded only for a relevant branch.
- **Asset:** material used in output without needing to become prompt context.
- **Script:** deterministic code for repeated or reliability-sensitive work.
- **Progressive disclosure:** exposing detail only when the current branch
  needs it.
- **Single source of truth:** one authoritative home for each instruction or
  contract.

## Steering

- **Degree of freedom:** how much implementation judgment the workflow leaves
  to the model.
- **Guardrail:** a constraint justified by concrete safety, correctness, or
  recovery cost.
- **Rubric:** criteria for evaluating output without prescribing one exact
  implementation.
- **Completion evidence:** observable proof that the requested outcome was
  reached.

## Maintenance failures

- **Duplication:** the same instruction in more than one place.
- **Sediment:** obsolete guidance retained because deletion feels risky.
- **Sprawl:** entrypoint detail that belongs behind a branch-specific reference.
- **No-op:** an instruction that does not change behavior from the model's
  default.
- **Overconstraint:** a fixed procedure applied where context admits safer or
  better alternatives.
