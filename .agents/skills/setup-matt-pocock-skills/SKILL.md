---
name: setup-matt-pocock-skills
description: Configure repository-local tracker, triage, and domain-document conventions needed by the Matt Pocock engineering skills.
---

# Configure the repository

1. Inspect existing tracker conventions, remotes, domain documents, ADRs, and
   skill configuration. Reuse established structure.
2. Determine which configuration the installed workflows actually need:
   issue-tracker guidance, triage labels, domain-document guidance, or a subset.
3. Infer unambiguous choices from the repository. Ask only about choices that
   materially change behavior, and present a recommendation with the tradeoff.
4. Show the proposed repository-local files before writing when the choice is
   material or the files already contain user decisions.
5. Write the smallest idempotent configuration that satisfies the selected
   workflows. Do not add skill inventories or workflow prose to `AGENTS.md` or
   `CLAUDE.md`; keep those files lightweight and point workflows at dedicated
   configuration instead.
6. Report what was configured and what remains intentionally unconfigured.

Use the existing seed files only for the selected branch:

- GitHub: [issue-tracker-github.md](issue-tracker-github.md)
- GitLab: [issue-tracker-gitlab.md](issue-tracker-gitlab.md)
- Local Markdown: [issue-tracker-local.md](issue-tracker-local.md)
- Triage labels: [triage-labels.md](triage-labels.md)
- Domain documents: [domain.md](domain.md)

Adapt templates to the repository; they are references, not required output
shapes.
