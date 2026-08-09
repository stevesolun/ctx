---
name: resolving-merge-conflicts
description: Resolve an in-progress Git merge or rebase conflict while preserving the relevant intent of both sides. Use when conflict markers or an interrupted merge/rebase need investigation and resolution.
---

# Resolve Merge Conflicts

1. Inspect the current Git state, the operation in progress, its goal, and every
   conflicting file.
2. Reconstruct each side's intent from the code, nearby tests, history, commit
   messages, and linked work items that are available and useful.
3. Resolve each hunk by preserving compatible intent. Where intents conflict,
   prefer the merge's stated goal and current repository contracts; avoid
   inventing unrelated behavior.
4. Run focused checks for the affected surface, then broader checks when risk or
   repository guidance warrants them.
5. Unless the user requested advice or review only, stage resolved files and
   continue the current merge or rebase when it is safe to do so. Report what was
   resolved, what was verified, and any remaining uncertainty.

Pause rather than guess when neither intent can be preserved safely. Aborting or
restarting can be the correct recovery when the base is wrong, the operation is
contaminated, or the user prefers a clean retry; explain the trade-off before
taking that destructive step.
