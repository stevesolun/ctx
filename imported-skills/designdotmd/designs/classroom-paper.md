---
version: alpha
name: Classroom Paper
description: Composition notebook, red-line margin, graphite.
colors:
  primary: "#22201D"
  secondary: "#7B766D"
  tertiary: "#C4251C"
  neutral: "#F7F1E3"
  surface: "#FFFBEF"
  on-primary: "#FFFBEF"
typography:
  display:
    fontFamily: Caveat
    fontSize: 4rem
    fontWeight: 700
  h1:
    fontFamily: Source Serif 4
    fontSize: 2.25rem
    fontWeight: 600
  body:
    fontFamily: Source Serif 4
    fontSize: 1.05rem
    lineHeight: 1.7
  label:
    fontFamily: Source Sans 3
    fontSize: 0.75rem
    fontWeight: 600
    letterSpacing: "0.08em"
rounded:
  sm: 4px
  md: 8px
  lg: 14px
spacing:
  sm: 8px
  md: 16px
  lg: 32px
components:
  button-primary:
    backgroundColor: "{colors.tertiary}"
    textColor: "{colors.on-primary}"
    rounded: "{rounded.md}"
    padding: 12px 20px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 24px
---
## Overview

A learning-platform system that feels like a good notebook.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#22201D`):** Headlines and core text.
- **Secondary (`#7B766D`):** Borders, captions, and metadata.
- **Tertiary (`#C4251C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F7F1E3`):** The page foundation.

## Typography

- **display:** Caveat 4rem
- **h1:** Source Serif 4 2.25rem
- **body:** Source Serif 4 1.05rem
- **label:** Source Sans 3 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
