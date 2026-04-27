---
version: alpha
name: Surf Daybreak
description: Surf forecast: ocean teal, dawn coral, seafoam.
colors:
  primary: "#0C3340"
  secondary: "#5A8894"
  tertiary: "#FF7A5C"
  neutral: "#DFF1EC"
  surface: "#F3FAF7"
  on-primary: "#FFFFFF"
typography:
  display:
    fontFamily: Recoleta
    fontSize: 4rem
    fontWeight: 600
    letterSpacing: "-0.02em"
  h1:
    fontFamily: Fraunces
    fontSize: 2.3rem
    fontWeight: 500
  body:
    fontFamily: Inter
    fontSize: 1rem
    lineHeight: 1.6
  label:
    fontFamily: Inter
    fontSize: 0.72rem
    fontWeight: 600
    letterSpacing: "0.12em"
rounded:
  sm: 10px
  md: 18px
  lg: 28px
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

A surf-forecast palette: ocean teal primary, dawn-coral accent, seafoam surface.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0C3340`):** Headlines and core text.
- **Secondary (`#5A8894`):** Borders, captions, and metadata.
- **Tertiary (`#FF7A5C`):** The sole driver for interaction. Reserve it.
- **Neutral (`#DFF1EC`):** The page foundation.

## Typography

- **display:** Recoleta 4rem
- **h1:** Fraunces 2.3rem
- **body:** Inter 1rem
- **label:** Inter 0.72rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
