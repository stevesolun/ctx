# Historical dataset publication

**Automatic CTX-owned dataset publication is retired.** CTX now distributes
the engine, knowledge-graph/wiki structure, and import/query infrastructure.
It does not maintain or ship a public collection of downloaded skills,
agents, or MCP servers.

The historical [Hugging Face dataset](https://huggingface.co/datasets/Stevesolun/ctx)
and older GitHub graph assets describe previous releases. Their existence does
not make them current defaults. A normal installation must not fetch them,
and a software release must not depend on synchronizing that corpus.

This page is retained so old links have an honest destination. Historical audit
and release evidence remain in repository history; removing the current
publication workflow does not erase remote historical snapshots or revoke
previously exposed credentials. The owner must handle credential revocation
separately.

## Publish your own knowledge only by choice

Your organization can keep its wiki and graph in storage it controls.
Review content, licenses, credentials, and permissions before sharing.
No recommendation or graph record authorizes uploading private knowledge.
CTX does not automatically upload your wiki to Hugging Face or GitHub.

Start with [entity onboarding](entity-onboarding.md) and
[the knowledge graph guide](knowledge-graph.md). The optional `ctx fit`
evaluator does not require a hosted dataset.
