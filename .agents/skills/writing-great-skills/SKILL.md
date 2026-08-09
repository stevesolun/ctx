---
name: writing-great-skills
description: Design or revise concise skills with clear triggers, proportional constraints, progressive disclosure, and realistic validation.
---

# Write an effective skill

1. Define the job, realistic trigger prompts, non-trigger prompts, expected
   output, and material failure modes.
2. Choose the degree of freedom:
   - Use judgment-based guidance when context admits several good approaches.
   - Use parameters or pseudocode when a preferred pattern still needs
     adaptation.
   - Use deterministic scripts or hard gates only when reliability, safety, or
     repeatability justifies the constraint.
3. Keep metadata concise and discriminative. Put trigger conditions in the
   description; put execution guidance in the body.
4. Assume the model already understands general software work. Preserve only
   domain knowledge, workflow choices, tool interfaces, and opinions that
   materially change behavior.
5. Keep the entrypoint legible. Move branch-specific detail, catalogues,
   templates, and examples to direct references; avoid deep reference chains.
6. Prefer positive target behavior and surrounding-context judgment over
   blanket prohibitions. State exact constraints where mistakes would be
   costly or hard to recover.
7. Validate syntax and links, then test both triggering and output quality with
   fresh, realistic prompts. Compare against a no-skill baseline when the
   skill's value is uncertain.
8. Prune repetition, stale material, and instructions that do not change model
   behavior.

Read [GLOSSARY.md](GLOSSARY.md) when shared vocabulary helps discuss invocation,
information hierarchy, steering, or pruning.
