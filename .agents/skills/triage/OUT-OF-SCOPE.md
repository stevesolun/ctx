# Out-of-scope guide

Use `.out-of-scope/` only when the repository intentionally maintains durable
records of rejected enhancement concepts. It is not a default requirement for
closing issues.

Before recording a rejection, confirm that:

- the request is an enhancement rather than a bug;
- it is genuinely rejected, not merely deferred or already implemented; and
- the reason is durable enough to help evaluate a future request.

Prefer one record per concept so related requests share the same reasoning. A
compact record can contain:

```markdown
# Concept

## Decision

What is out of scope and the durable reason.

## Prior requests

- Link to the issue or pull request
```

Match future requests by concept, not only keywords. Surface the prior decision
as evidence, while allowing the maintainer to reconsider it. If a decision
changes, update or remove the record according to repository convention; do not
assume historical issues must be reopened.
