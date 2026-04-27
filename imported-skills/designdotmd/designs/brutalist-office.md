---
version: alpha
name: Brutalist Office
description: Concrete, grids, and a single warning-yellow.
colors:
  primary: "#0A0A0A"
  secondary: "#4B4B4B"
  tertiary: "#E8FF00"
  neutral: "#E8E6E1"
  surface: "#FFFFFF"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: JetBrains Mono
    fontSize: 4rem
    fontWeight: 700
    letterSpacing: "-0.04em"
  h1:
    fontFamily: JetBrains Mono
    fontSize: 2rem
    fontWeight: 700
  body:
    fontFamily: JetBrains Mono
    fontSize: 0.95rem
    lineHeight: 1.5
  label:
    fontFamily: JetBrains Mono
    fontSize: 0.75rem
    letterSpacing: "0"
rounded:
  sm: 0px
  md: 0px
  lg: 0px
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

Unapologetic grids, monospace everywhere, no shadows. A single electric yellow punches through the slab.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0A0A0A`):** Headlines and core text.
- **Secondary (`#4B4B4B`):** Borders, captions, and metadata.
- **Tertiary (`#E8FF00`):** The sole driver for interaction. Reserve it.
- **Neutral (`#E8E6E1`):** The page foundation.

## Typography

- **display:** JetBrains Mono 4rem
- **h1:** JetBrains Mono 2rem
- **body:** JetBrains Mono 0.95rem
- **label:** JetBrains Mono 0.75rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
