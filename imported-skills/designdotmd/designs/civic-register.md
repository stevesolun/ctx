---
version: alpha
name: Civic Register
description: Civic site: gov navy, paper beige, official serif.
colors:
  primary: "#0A2540"
  secondary: "#5B6E81"
  tertiary: "#B91D1D"
  neutral: "#F2EFE6"
  surface: "#FBF8F0"
  on-primary: "#FBF8F0"
typography:
  display:
    fontFamily: Source Serif 4
    fontSize: 3.75rem
    fontWeight: 600
    letterSpacing: "-0.01em"
  h1:
    fontFamily: Source Serif 4
    fontSize: 2rem
    fontWeight: 600
  body:
    fontFamily: Source Sans 3
    fontSize: 1rem
    lineHeight: 1.65
  label:
    fontFamily: Source Sans 3
    fontSize: 0.74rem
    fontWeight: 600
    letterSpacing: "0.06em"
rounded:
  sm: 2px
  md: 4px
  lg: 6px
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

A civic/government palette: official navy, paper beige, accessible serif and structured tables.

## Colors

The palette is built around high-contrast neutrals and a single accent that drives interaction.

- **Primary (`#0A2540`):** Headlines and core text.
- **Secondary (`#5B6E81`):** Borders, captions, and metadata.
- **Tertiary (`#B91D1D`):** The sole driver for interaction. Reserve it.
- **Neutral (`#F2EFE6`):** The page foundation.

## Typography

- **display:** Source Serif 4 3.75rem
- **h1:** Source Serif 4 2rem
- **body:** Source Sans 3 1rem
- **label:** Source Sans 3 0.74rem

## Do's and Don'ts

- **Do** use Tertiary for exactly one action per screen.
- **Do** let Neutral carry the composition — negative space is a feature.
- **Don't** introduce gradients. This system is flat on purpose.
- **Don't** mix Tertiary with alternate accents; the single-accent rule is load-bearing.
