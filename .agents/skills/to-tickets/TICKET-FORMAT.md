# Ticket format

Adapt this format to the configured tracker.

## Title

Name the observable outcome, not a layer or implementation activity.

## Outcome

Describe the narrow behavior this ticket makes available and who benefits.

## Acceptance criteria

- State independently verifiable success and relevant failure behavior.
- Include compatibility or migration conditions when they matter.

## Blocked by

Reference only tickets whose completion is a genuine prerequisite. Use `None`
when the ticket can start immediately.

## Scope

Record important exclusions or constraints. Link the parent spec, issue, ADR, or
prototype where useful.

## Wide migrations

When a mechanical change cannot land as one independently green slice, consider:

1. **Expand:** introduce the new form alongside the old.
2. **Migrate:** move callers in coherent batches.
3. **Contract:** remove the old form after callers are gone.

Make the dependency edges explicit and add an integration checkpoint only when
individual batches cannot provide trustworthy evidence.
