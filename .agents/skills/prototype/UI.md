# UI Prototype

A UI prototype compares visual or interaction ideas with enough surrounding
context to make the trade-offs visible.

## Workflow

1. State the design question and the constraints every option must satisfy.
2. Choose a comparison surface:
   - embed variants in an existing page when its real data and density are
     important and the change is safely isolated;
   - use a clearly marked prototype route or local artifact when isolation is
     more valuable.
3. Produce enough structurally different variants to expose the decision,
   usually two or three. Vary hierarchy, layout, and primary affordance rather
   than only color or copy.
4. Reuse the project's component library, styles, and representative data.
5. Make comparison easy. A URL parameter and small variant switcher are useful
   for an in-app prototype; separate local pages or screenshots may be cheaper
   for a narrower question.
6. Run the prototype in its actual rendering environment and inspect the
   relevant viewport sizes and interactions.
7. Give the user the URL or command, label the variants, and summarize the
   trade-offs without choosing for them unless they requested a recommendation.

## Optional in-app switcher

When persistent, side-by-side browser evaluation is useful:

- Select variants through a shareable URL parameter.
- Provide compact previous/next controls and a clear variant label.
- Preserve existing data fetching outside the variant-specific rendering.
- Avoid intercepting keyboard shortcuts while an input is focused.
- Keep prototype controls out of production builds.

## Guardrails

- Do not let shared prototype code force variants into the same structure.
- Stub destructive mutations unless the interaction with the real backend is the
  question under test and can be exercised safely.
- Treat a selected variant as design evidence, not automatically as
  production-ready code.
- Remove or preserve prototype machinery only when the user asks for that next
  step.
