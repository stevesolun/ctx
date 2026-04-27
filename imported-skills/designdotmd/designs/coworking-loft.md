---
version: alpha
name: Coworking Loft
description: Loft coworking: warm concrete, mustard, exposed brick.
colors:
  primary: "#1C1A16"
  secondary: "#7E7A70"
  tertiary: "#D9A02C"
  neutral: "#EFE9DD"
  surface: "#F8F2E6"
  on-primary: "#F8F2E6"
typography:
  display:
    fontFamily: Space Grotesk
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.03em"
  h1:
    fontFamily: Space Grotesk
    fontSize: 2.1rem
    fontWeight: 600
  body:
    fontFamily: Inter
    fontSize: 0.98rem
    lineHeight: 1.6
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.72rem
    letterSpacing: "0.08em"
rounded:
  sm: 3px
  md: 6px
  lg: 10px
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

A coworking-loft palette: warm concrete surfaces, mustard primary, exposed-brick secondary.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#1C1A16`):** Headlines and core text.
- **Secondary (`#7E7A70`):** Borders, captions, and metadata.
- **Tertiary (`#D9A02C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#EFE9DD`):** The page foundation.

## Typography

- **display:** Space Grotesk 4rem
- **h1:** Space Grotesk 2.1rem
- **body:** Inter 0.98rem
- **label:** JetBrains Mono 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
