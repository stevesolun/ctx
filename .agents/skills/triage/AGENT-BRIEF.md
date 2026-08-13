# Agent brief guide

Use a brief when an issue or PR is ready for another agent and the existing
discussion does not already form a durable implementation contract.

Describe behavior rather than a brittle sequence of edits. Include the
interfaces or contracts that matter, but avoid line numbers and incidental file
layout. Distinguish work that starts from nothing from work that completes an
existing pull-request diff.

An effective brief usually contains:

```markdown
## Agent Brief

**Summary:** One-line outcome

**Current behavior:** What happens now or what the existing diff already does.

**Desired behavior:** Observable success, relevant failures, and edge cases.

**Key contracts:** Interfaces, data shapes, or compatibility constraints that
must remain true.

**Acceptance criteria:**
- [ ] Independently verifiable outcome

**Out of scope:** Adjacent work that should not be inferred.
```

Adapt the structure to the item. Omit empty sections and add context only when
it will remain useful if the code moves. Acceptance criteria should be concrete
enough for a fresh agent to determine completion without dictating an
unnecessary implementation.
